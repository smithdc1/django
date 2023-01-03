import gzip
import re
import secrets
import unicodedata
from collections import deque
from gzip import GzipFile
from gzip import compress as gzip_compress
from html import escape
from html.parser import HTMLParser
from io import BytesIO

from django.core.exceptions import SuspiciousFileOperation
from django.utils.functional import SimpleLazyObject, keep_lazy_text, lazy
from django.utils.regex_helper import _lazy_re_compile
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, pgettext


@keep_lazy_text
def capfirst(x):
    """Capitalize the first letter of a string."""
    if not x:
        return x
    if not isinstance(x, str):
        x = str(x)
    return x[0].upper() + x[1:]


# Set up regular expressions
re_words = _lazy_re_compile(r"<[^>]+?>|([^<>\s]+)", re.S)
re_chars = _lazy_re_compile(r"<[^>]+?>|(.)", re.S)
re_tag = _lazy_re_compile(r"<(/)?(\S+?)(?:(\s*/)|\s.*?)?>", re.S)
re_newlines = _lazy_re_compile(r"\r\n|\r")  # Used in normalize_newlines
re_camel_case = _lazy_re_compile(r"(((?<=[a-z])[A-Z])|([A-Z](?![A-Z]|$)))")


@keep_lazy_text
def wrap(text, width):
    """
    A word-wrap function that preserves existing line breaks. Expects that
    existing line breaks are posix newlines.

    Preserve all white space except added line breaks consume the space on
    which they break the line.

    Don't wrap long words, thus the output text may have lines longer than
    ``width``.
    """

    def _generator():
        for line in text.splitlines(True):  # True keeps trailing linebreaks
            max_width = min((line.endswith("\n") and width + 1 or width), width)
            while len(line) > max_width:
                space = line[: max_width + 1].rfind(" ") + 1
                if space == 0:
                    space = line.find(" ") + 1
                    if space == 0:
                        yield line
                        line = ""
                        break
                yield "%s\n" % line[: space - 1]
                line = line[space:]
                max_width = min((line.endswith("\n") and width + 1 or width), width)
            if line:
                yield line

    return "".join(_generator())


class DjangoHTMLParser(HTMLParser):
    class TruncationCompleted(Exception):
        pass

    void_elements = {  # https://html.spec.whatwg.org/#void-elements
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, *, convert_charrefs=True):
        super().__init__(convert_charrefs=convert_charrefs)
        self.tags = deque()
        self.output = ""

    def handle_starttag(self, tag, attrs):
        self.output += self.get_starttag_text()
        if tag not in self.void_elements:
            self.tags.appendleft(tag)

    def handle_endtag(self, tag):
        if tag not in self.void_elements:
            self.output += f"</{tag}>"
            self.tags.remove(tag)

    def handle_data(self, data):
        if self.func == "words":
            data = re.split(r"(?<=\S)\s(?=\S)", data)
            output = escape(" ".join(data[: self.num]))
        else:
            output = escape("".join(data[: self.num]))

        data_len = len(data)
        if self.num < data_len:
            if self.func == "words" and self.truncate and self.truncate[0] == " ":
                output = output.strip()
            self.output += Truncator.add_truncation_text(output, self.truncate)
            self.output += "".join([f"</{tag}>" for tag in self.tags])
            raise self.TruncationCompleted()
        self.num -= data_len
        self.output += output

    def _truncate_html(self, func, text, num, truncate):
        if func not in ("words", "chars"):
            raise AttributeError(f"func must be 'words' or 'chars', got '{func}'.")
        if num <= 0:
            if func == "words":
                return ""
            else:
                return truncate
        self.func = func
        self.num = num
        self.truncate = truncate
        try:
            self.feed(text)
        except self.TruncationCompleted:
            pass
        return self.output


class Truncator(SimpleLazyObject):
    """
    An object used to truncate text, either by characters or words.
    """

    def __init__(self, text):
        super().__init__(lambda: str(text))

    @classmethod
    def add_truncation_text(cls, text, truncate=None):
        if truncate is None:
            truncate = pgettext(
                "String to return when truncating text", "%(truncated_text)s…"
            )
        if "%(truncated_text)s" in truncate:
            return truncate % {"truncated_text": text}
        # The truncation text didn't contain the %(truncated_text)s string
        # replacement argument so just append it to the text.
        if text.endswith(truncate):
            # But don't append the truncation text if the current text already
            # ends in this.
            return text
        return "%s%s" % (text, truncate)

    def chars(self, num, truncate=None, html=False):
        """
        Return the text truncated to be no longer than the specified number
        of characters.

        `truncate` specifies what should be used to notify that the string has
        been truncated, defaulting to a translatable string of an ellipsis.
        """
        self._setup()
        length = int(num)
        text = unicodedata.normalize("NFC", self._wrapped)

        # Calculate the length to truncate to (max length - end_text length)
        truncate_len = length
        truncate = self.add_truncation_text("", truncate)
        for char in truncate:
            if not unicodedata.combining(char):
                truncate_len -= 1
                if truncate_len == 0:
                    break
        if html:
            parser = DjangoHTMLParser()
            return parser._truncate_html("chars", text, truncate_len, truncate)
        return self._text_chars(length, truncate, text, truncate_len)

    def _text_chars(self, length, truncate, text, truncate_len):
        """Truncate a string after a certain number of chars."""
        s_len = 0
        end_index = None
        for i, char in enumerate(text):
            if unicodedata.combining(char):
                # Don't consider combining characters
                # as adding to the string length
                continue
            s_len += 1
            if end_index is None and s_len > truncate_len:
                end_index = i
            if s_len > length:
                # Return the truncated string
                return self.add_truncation_text(text[: end_index or 0], truncate)

        # Return the original string since no truncation was necessary
        return text

    def words(self, num, truncate=None, html=False):
        """
        Truncate a string after a certain number of words. `truncate` specifies
        what should be used to notify that the string has been truncated,
        defaulting to ellipsis.
        """
        self._setup()
        length = int(num)
        if html:
            parser = DjangoHTMLParser()
            return parser._truncate_html("words", self._wrapped, num, truncate)
        return self._text_words(length, truncate)

    def _text_words(self, length, truncate):
        """
        Truncate a string after a certain number of words.

        Strip newlines in the string.
        """
        words = self._wrapped.split()
        if len(words) > length:
            words = words[:length]
            return self.add_truncation_text(" ".join(words), truncate)
        return " ".join(words)


@keep_lazy_text
def get_valid_filename(name):
    """
    Return the given string converted to a string that can be used for a clean
    filename. Remove leading and trailing spaces; convert other spaces to
    underscores; and remove anything that is not an alphanumeric, dash,
    underscore, or dot.
    >>> get_valid_filename("john's portrait in 2004.jpg")
    'johns_portrait_in_2004.jpg'
    """
    s = str(name).strip().replace(" ", "_")
    s = re.sub(r"(?u)[^-\w.]", "", s)
    if s in {"", ".", ".."}:
        raise SuspiciousFileOperation("Could not derive file name from '%s'" % name)
    return s


@keep_lazy_text
def get_text_list(list_, last_word=gettext_lazy("or")):
    """
    >>> get_text_list(['a', 'b', 'c', 'd'])
    'a, b, c or d'
    >>> get_text_list(['a', 'b', 'c'], 'and')
    'a, b and c'
    >>> get_text_list(['a', 'b'], 'and')
    'a and b'
    >>> get_text_list(['a'])
    'a'
    >>> get_text_list([])
    ''
    """
    if not list_:
        return ""
    if len(list_) == 1:
        return str(list_[0])
    return "%s %s %s" % (
        # Translators: This string is used as a separator between list elements
        _(", ").join(str(i) for i in list_[:-1]),
        str(last_word),
        str(list_[-1]),
    )


@keep_lazy_text
def normalize_newlines(text):
    """Normalize CRLF and CR newlines to just LF."""
    return re_newlines.sub("\n", str(text))


@keep_lazy_text
def phone2numeric(phone):
    """Convert a phone number with letters into its numeric equivalent."""
    char2number = {
        "a": "2",
        "b": "2",
        "c": "2",
        "d": "3",
        "e": "3",
        "f": "3",
        "g": "4",
        "h": "4",
        "i": "4",
        "j": "5",
        "k": "5",
        "l": "5",
        "m": "6",
        "n": "6",
        "o": "6",
        "p": "7",
        "q": "7",
        "r": "7",
        "s": "7",
        "t": "8",
        "u": "8",
        "v": "8",
        "w": "9",
        "x": "9",
        "y": "9",
        "z": "9",
    }
    return "".join(char2number.get(c, c) for c in phone.lower())


def _get_random_filename(max_random_bytes):
    return b"a" * secrets.randbelow(max_random_bytes)


def compress_string(s, *, max_random_bytes=None):
    compressed_data = gzip_compress(s, compresslevel=6, mtime=0)

    if not max_random_bytes:
        return compressed_data

    compressed_view = memoryview(compressed_data)
    header = bytearray(compressed_view[:10])
    header[3] = gzip.FNAME

    filename = _get_random_filename(max_random_bytes) + b"\x00"

    return bytes(header) + filename + compressed_view[10:]


class StreamingBuffer(BytesIO):
    def read(self):
        ret = self.getvalue()
        self.seek(0)
        self.truncate()
        return ret


# Like compress_string, but for iterators of strings.
def compress_sequence(sequence, *, max_random_bytes=None):
    buf = StreamingBuffer()
    filename = _get_random_filename(max_random_bytes) if max_random_bytes else None
    with GzipFile(
        filename=filename, mode="wb", compresslevel=6, fileobj=buf, mtime=0
    ) as zfile:
        # Output headers...
        yield buf.read()
        for item in sequence:
            zfile.write(item)
            data = buf.read()
            if data:
                yield data
    yield buf.read()


# Expression to match some_token and some_token="with spaces" (and similarly
# for single-quoted strings).
smart_split_re = _lazy_re_compile(
    r"""
    ((?:
        [^\s'"]*
        (?:
            (?:"(?:[^"\\]|\\.)*" | '(?:[^'\\]|\\.)*')
            [^\s'"]*
        )+
    ) | \S+)
""",
    re.VERBOSE,
)


def smart_split(text):
    r"""
    Generator that splits a string by spaces, leaving quoted phrases together.
    Supports both single and double quotes, and supports escaping quotes with
    backslashes. In the output, strings will keep their initial and trailing
    quote marks and escaped quotes will remain escaped (the results can then
    be further processed with unescape_string_literal()).

    >>> list(smart_split(r'This is "a person\'s" test.'))
    ['This', 'is', '"a person\\\'s"', 'test.']
    >>> list(smart_split(r"Another 'person\'s' test."))
    ['Another', "'person\\'s'", 'test.']
    >>> list(smart_split(r'A "\"funky\" style" test.'))
    ['A', '"\\"funky\\" style"', 'test.']
    """
    for bit in smart_split_re.finditer(str(text)):
        yield bit[0]


@keep_lazy_text
def unescape_string_literal(s):
    r"""
    Convert quoted string literals to unquoted strings with escaped quotes and
    backslashes unquoted::

        >>> unescape_string_literal('"abc"')
        'abc'
        >>> unescape_string_literal("'abc'")
        'abc'
        >>> unescape_string_literal('"a \"bc\""')
        'a "bc"'
        >>> unescape_string_literal("'\'ab\' c'")
        "'ab' c"
    """
    if not s or s[0] not in "\"'" or s[-1] != s[0]:
        raise ValueError("Not a string literal: %r" % s)
    quote = s[0]
    return s[1:-1].replace(r"\%s" % quote, quote).replace(r"\\", "\\")


@keep_lazy_text
def slugify(value, allow_unicode=False):
    """
    Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
    dashes to single dashes. Remove characters that aren't alphanumerics,
    underscores, or hyphens. Convert to lowercase. Also strip leading and
    trailing whitespace, dashes, and underscores.
    """
    value = str(value)
    if allow_unicode:
        value = unicodedata.normalize("NFKC", value)
    else:
        value = (
            unicodedata.normalize("NFKD", value)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
    value = re.sub(r"[^\w\s-]", "", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-_")


def camel_case_to_spaces(value):
    """
    Split CamelCase and convert to lowercase. Strip surrounding whitespace.
    """
    return re_camel_case.sub(r" \1", value).strip().lower()


def _format_lazy(format_string, *args, **kwargs):
    """
    Apply str.format() on 'format_string' where format_string, args,
    and/or kwargs might be lazy.
    """
    return format_string.format(*args, **kwargs)


format_lazy = lazy(_format_lazy, str)
