# Copyright © 2019 Clément Pit-Claudel
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from functools import reduce
from typing import Any, DefaultDict, Dict, Iterable, \
    NamedTuple, NoReturn, Optional, Tuple, Union

from collections import deque, namedtuple, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import import_module
from io import TextIOWrapper
from pathlib import Path
from shlex import quote
from shutil import which
from subprocess import PIPE
import os
import re
import subprocess
import sys
import textwrap

DEBUG = False
TRACEBACK = False

class UnexpectedError(ValueError):
    pass

def indent(text, prefix):
    if prefix.isspace():
        return textwrap.indent(text, prefix)
    text = re.sub("^(?!$)", prefix, text, flags=re.MULTILINE)
    return re.sub("^$", prefix.rstrip(), text, flags=re.MULTILINE)

def debug(v, prefix):
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", errors="replace")
    if DEBUG:
        print(indent(str(v).rstrip(), prefix), flush=True)

class DriverInfo(namedtuple("DriverInfo", "name version")):
    def fmt(self, include_version_info=True):
        return "{} v{}".format(self.name, self.version) if include_version_info else self.name

Hypothesis = namedtuple("Hypothesis", "names body type")
Goal = namedtuple("Goal", "name conclusion hypotheses")
Message = namedtuple("Message", "contents")
TypeInfo = namedtuple("TypeInfo", "name type")
Sentence = namedtuple("Sentence", "contents messages goals")
Text = namedtuple("Text", "contents")
FragmentToken = namedtuple("FragmentToken", "raw typeinfo docstring link semanticType", defaults=("", None, None, None, None))
Fragment = Union[Text, Sentence]

class Enriched():
    def __new__(cls, *args, **kwargs):
        if len(args) < len(getattr(super(), "_fields", ())):
            # Don't repeat fields given by position (it breaks pickle & deepcopy)
            kwargs = {"props": {}, **kwargs}
        return super().__new__(cls, *args, **kwargs)
    @property
    def ids(self):
        return getattr(self, "props", {}).setdefault("ids", [])
    @property
    def markers(self):
        return getattr(self, "props", {}).setdefault("markers", [])

def _enrich(nt):
    # LATER: Use dataclass + multiple inheritance; change `ids` and `markers` to
    # mutable `id` and `marker` fields.
    name = "Rich" + nt.__name__
    fields = nt._fields + ("props",)
    # Using ``type`` this way ensures compatibility with pickling
    return type(name, (Enriched, namedtuple(name, fields)),
                {"__slots__": ()})

Goals = namedtuple("Goals", "goals")
Messages = namedtuple("Messages", "messages")

class Names(list): pass
RichHypothesis = _enrich(Hypothesis)
RichGoal = _enrich(Goal)
RichMessage = _enrich(Message)
RichCode = _enrich(namedtuple("Code", "contents"))
RichSentence = _enrich(namedtuple("Sentence", "input outputs annots prefixes suffixes"))

def b16(i):
    return hex(i)[len("0x"):]

class Gensym():
    # Having a global table of counters ensures that creating multiple Gensym
    # instances in the same session doesn't cause collisions
    GENSYM_COUNTERS: Dict[str, DefaultDict[str, int]] = {}

    def __init__(self, stem):
        self.stem = stem
        self.counters = self.GENSYM_COUNTERS.setdefault(stem, defaultdict(lambda: -1))

    def __call__(self, prefix):
        self.counters[prefix] += 1
        return self.stem + prefix + b16(self.counters[prefix])

@contextmanager
def nullctx():
    yield

@contextmanager
def cwd(wd: Union[str, os.PathLike]):
    old_cwd = os.getcwd()
    os.chdir(wd)
    try:
        yield
    finally:
        os.chdir(old_cwd)

class Backend:
    def __init__(self, highlighter):
        self.highlighter = highlighter

    def gen_fragment(self, fr): raise NotImplementedError()
    def gen_hyp(self, hyp): raise NotImplementedError()
    def gen_goal(self, goal): raise NotImplementedError()
    def gen_message(self, message): raise NotImplementedError()
    def highlight(self, s): raise NotImplementedError()
    def gen_names(self, names): raise NotImplementedError()
    def gen_code(self, code): raise NotImplementedError()
    def gen_txt(self, s): raise NotImplementedError()
    def gen_token(self, token): raise NotImplementedError()
    def gen_typeinfo(self, typeinfo): raise NotImplementedError()

    def highlight_enriched(self, obj):
        lang = obj.props.get("lang")
        with self.highlighter.override(lang=lang) if lang else nullctx():
            if isinstance(obj.contents, str):
                return self.highlight(obj.contents)
            else:
                list = []
                for token in obj.contents.tokens:
                    self.gen_token(token)
                return list


    def _gen_any(self, obj):
        if isinstance(obj, (Text, RichSentence)):
            self.gen_fragment(obj)
        elif isinstance(obj, RichHypothesis):
            self.gen_hyp(obj)
        elif isinstance(obj, RichGoal):
            self.gen_goal(obj)
        elif isinstance(obj, RichMessage):
            self.gen_message(obj)
        elif isinstance(obj, RichCode):
            self.gen_code(obj)
        elif isinstance(obj, Names):
            self.gen_names(obj)
        elif isinstance(obj, FragmentToken):
            self.gen_token(obj)
        elif isinstance(obj, TypeInfo):
            self.gen_typeinfo(obj)
        elif isinstance(obj, str):
            self.gen_txt(obj)
        else:
            raise TypeError("Unexpected object type: {}".format(type(obj)))

class Asset(str):
    def __new__(cls, fname, _gen=None):
        return super().__new__(cls, fname)

    def __init__(self, _fname, gen=lambda _: ""):
        super().__init__()
        self.gen = gen

class Position(NamedTuple):
    fpath: Union[str, Path]
    line: int
    col: int

    def as_range(self):
        return Range(self, None)

class Range(NamedTuple):
    beg: Position
    end: Optional[Position]

    def as_header(self):
        assert self.end is None or self.beg.fpath == self.end.fpath
        beg = "{}:{}".format(self.beg.line, self.beg.col)
        end = "{}:{}".format(self.end.line, self.end.col) if self.end else ""
        pos = ("({})-({})" if end else "{}:{}").format(beg, end)
        return "{}:{}:".format(self.beg.fpath or "<unknown>", pos)

class PosStr(str):
    def __new__(cls, s, *_args):
        return super().__new__(cls, s)

    def __init__(self, _s, pos, col_offset):
        super().__init__()
        self.pos, self.col_offset = pos, col_offset

class View(bytes):
    def __getitem__(self, key):
        return memoryview(self).__getitem__(key)

    def __init__(self, s):
        super().__init__()
        self.s = s

class PosView(View):
    NL = b"\n"

    def __new__(cls, s):
        bs = s.encode("utf-8")
        # https://stackoverflow.com/questions/20221858/
        return super().__new__(cls, bs) if isinstance(s, PosStr) else View(bs)

    def __init__(self, s):
        super().__init__(s)
        self.pos, self.col_offset = s.pos, s.col_offset

    def __getitem__(self, key):
        return memoryview(self).__getitem__(key)

    def translate_offset(self, offset):
        r"""Translate a character-based `offset` into a (line, column) pair.
        Columns are 1-based.

        >>> text = "abc\ndef\nghi"
        >>> s = PosView(PosStr(text, Position("f", 3, 2), 5))
        >>> s.translate_offset(0)
        Position(fpath='f', line=3, col=2)
        >>> s.translate_offset(10) # col=3, + offset (5) = 8
        Position(fpath='f', line=5, col=8)
        """
        nl = self.rfind(self.NL, 0, offset)
        if nl == -1: # First line
            line, col = self.pos.line, self.pos.col + offset
        else:
            line = self.pos.line + self.count(self.NL, 0, offset)
            prefix = bytes(self[nl+1:offset]).decode("utf-8", 'ignore')
            col = 1 + self.col_offset + len(prefix)
        return Position(self.pos.fpath, line, col)

    def translate_span(self, beg, end):
        return Range(self.translate_offset(beg),
                     self.translate_offset(end))

Positioned = namedtuple("Positioned", "beg end e")

class Document:
    """A utility class to handle conversions to and from a list of chunks.

    This is useful to recover chunk boundaries for provers that concatenate all
    chunks before processing them.
    """
    def __init__(self, chunks, chunk_separator):
        self.chunks = list(chunks)
        self.with_separator = [c + chunk_separator for c in self.chunks]
        self.contents = chunk_separator[0:0].join(self.with_separator)
        self.separator = chunk_separator
        self._bol_offsets = None

    def __getitem__(self, index):
        return self.contents.__getitem__(index)

    def __len__(self):
        return len(self.contents)

    def recover_chunks(self, fragments):
        grouped = self._recover_chunks(self.with_separator, fragments)
        return self.strip_separators(grouped, self.separator)

    @property
    def bol_offsets(self):
        if self._bol_offsets is None:
            bol = "^" if isinstance(self.contents, str) else b"^"
            matches = re.finditer(bol, self.contents, re.MULTILINE)
            self._bol_offsets = [m.start() for m in matches]
        return self._bol_offsets

    def offset2pos(self, offset) -> Tuple[int, int]:
        import bisect
        zline = bisect.bisect_right(self.bol_offsets, offset)
        bol = self.bol_offsets[zline - 1]
        return zline, offset - bol

    def pos2offset(self, line: int, col: int) -> int:
        return self.bol_offsets[line - 1] + col

    @staticmethod
    def intersperse_text_fragments(text, positioned_sentences):
        pos = 0
        for st in positioned_sentences:
            if pos < st.beg:
                yield Text(text[pos:st.beg])
            yield st.e
            pos = st.end
        if pos < len(text):
            yield Text(text[pos:len(text)])

    @staticmethod
    def with_boundaries(items: Iterable[Union[Sentence, Text, str]]):
        end = 0
        for item in items:
            beg, end = end, end + len(getattr(item, "contents", item))
            yield Positioned(beg, end, item)

    @staticmethod
    def split_fragment(fr: Fragment, cutoff):
        """Split `fr` at position `cutoff`.

        >>> Document.split_fragment(Text(contents=[FragmentToken(raw='ab'), FragmentToken(raw='cx'), FragmentToken(raw='yz')]), 3)
        (Text(contents=[FragmentToken(raw='ab'), FragmentToken(raw='c')]), Text(contents=[FragmentToken(raw='x'), FragmentToken(raw='yz')]))
        >>> Document.split_fragment(Sentence(contents=[FragmentToken(raw='ab'), FragmentToken(raw='cx'), FragmentToken(raw='yz')], [Message("out")], []), 3)
        (Sentence(contents=[FragmentToken(raw='ab'), FragmentToken(raw='c')], messages=[], goals=[]),
         Sentence(contents=[FragmentToken(raw='x'), FragmentToken(raw='yz')], messages=[Message(contents='out')], goals=[]))
        """
        (before, after) = fr.contents.split_at_pos(cutoff=cutoff)
        fr0: Fragment
        if isinstance(fr, Text):
            fr0 = Text(before)
        else:
            fr0 = Sentence(before, messages=[], goals=[])
        return fr0, fr._replace(contents=after)

    @classmethod
    def split_fragments(cls, fragments, cutoffs):
        """Split `fragments` at positions `cutoffs`.

        >>> list(Document.split_fragments([Text(contents=[FragmentToken(raw="abcdwxyz")])], [0, 2, 4, 5, 7]))
        [Text(contents=[FragmentToken(raw="ab")]), Text(contents=[FragmentToken(raw="cd")]),
         Text(contents=[FragmentToken(raw="w")]), Text(contents=[FragmentToken(raw="xy")]), Text(contents=[FragmentToken(raw="z")])]
        >>> list(Document.split_fragments([Text(contents=[FragmentToken(raw="abcd")]), Text(contents=[FragmentToken(raw="wxyz")])], [0, 2, 4, 5, 7]))
        [Text(contents=[FragmentToken(raw="ab")]), Text(contents=[FragmentToken(raw="cd")]),
         Text(contents=[FragmentToken(raw="w")]), Text(contents=[FragmentToken(raw="xy")]), Text(contents=[FragmentToken(raw="z")])]
        """
        fragments = deque(cls.with_boundaries(fragments))
        for cutoff in cutoffs:
            while fragments and fragments[0].end <= cutoff:
                yield fragments.popleft().e
            if fragments and fragments[0].beg < cutoff < fragments[0].end:
                before, after = cls.split_fragment(fragments[0].e, cutoff - fragments[0].beg)
                fragments[0] = fragments[0]._replace(beg=cutoff, e=after)
                yield before
        for fr in fragments:
            yield fr.e

    @classmethod
    def _recover_chunks(cls, chunks: Iterable[str],
                        fragments: Iterable[Union[Sentence, Text]]):
        frs = deque(cls.with_boundaries(fragments))
        for chunk in cls.with_boundaries(chunks):
            chunk_frs = []
            if frs:
                assert frs[0].beg >= chunk.beg
            while frs and frs[0].end <= chunk.end:
                chunk_frs.append(frs.popleft().e)
            if frs and frs[0].beg < chunk.end < frs[0].end:
                cutoff = chunk.end - frs[0].beg
                before, after = cls.split_fragment(frs[0].e, cutoff)
                frs[0] = frs[0]._replace(beg=chunk.end, e=after)
                chunk_frs.append(before)
            assert chunk.e == chunk.e[0:0].join(str(c.contents) for c in chunk_frs)
            yield chunk_frs
        assert not frs

    @classmethod
    def strip_separators(cls, grouped, separator):
        r"""Remove separator at end of each fragment in `grouped`.
        >>> list(Document.strip_separators([[Text("!"), Text("(* … *)\n")]], "\n"))
        [[Text(contents='!'), Text(contents='(* … *)')]]
        >>> list(Document.strip_separators([[Text("A"), Text("\n")]], "\n"))
        [[Text(contents='A')]]
        >>> list(Document.strip_separators([[Text("\n")]], "\n"))
        [[]]
        """
        for fragments in grouped:
            if fragments:
                assert fragments[-1].contents.endswith(separator)
                (contents, _) = fragments[-1].contents.split_at_pos(-len(separator))
                fragments[-1] = fragments[-1]._replace(contents=contents)
                if isinstance(fragments[-1], Text) and not contents:
                    fragments.pop()
            yield fragments

class EncodedDocument(Document):
    """Variant of ``Document`` in which positions are byte offsets, not char offsets."""
    def __init__(self, chunks, chunk_separator, encoding="utf-8"):
        super().__init__(chunks, chunk_separator)
        self.encoding = encoding
        self.contents = self.contents.encode(encoding)

    def __getitem__(self, index):
        return super().__getitem__(index).decode(self.encoding)

class Notification(NamedTuple):
    obj: Any
    message: str
    location: Optional[Range]
    level: int

class Observer:
    def _notify(self, n: Notification):
        raise NotImplementedError()

    def notify(self, obj: Any, message: str, location: Optional[Range], level: int):
        self._notify(Notification(obj, message, location, level))

class StderrObserver(Observer):
    def __init__(self):
        self.exit_code = 0

    def _notify(self, n: Notification):
        self.exit_code = max(self.exit_code, n.level)
        header = n.location.as_header() if n.location else "!!"
        message = n.message.rstrip().replace("\n", "\n   ")
        level_name = {2: "WARNING", 3: "ERROR"}.get(n.level, "??")
        sys.stderr.write("{} ({}/{}) {}\n".format(header, level_name, n.level, message))

PrettyPrinted = namedtuple("PrettyPrinted", "sid pp")
Contents = namedtuple("Contents", "tokens")

@dataclass(frozen=True)
class FragmentContent:
    tokens: list[FragmentToken]

    @staticmethod
    def create(val=[]):
        if isinstance(val, str):
            if val:
                return FragmentContent([FragmentToken(val, None, None)])
            else:
                return FragmentContent([])
        elif isinstance(val, FragmentToken):
            if val.raw:
                return FragmentContent([val])
            else:
                return FragmentContent([])
        elif isinstance(val, FragmentContent):
            return val
        elif isinstance(val, Contents):
            return FragmentContent(val.tokens)
        else:
            return FragmentContent(val)

    def __add__(self, other):
        if isinstance(other, FragmentContent):
            return FragmentContent(self.tokens + other.tokens)
        elif isinstance(other, FragmentToken):
            if not other.raw:
                return FragmentContent(self.tokens)
            return FragmentContent(self.tokens + [other])
        elif isinstance(other, list):
            return FragmentContent(self.tokens.append(other))

    def __len__(self):
        return reduce(lambda a, b: a + len(b.raw), self.tokens, 0)

    def __str__(self):
        return "".join(c.raw for c in self.tokens)

    def __eq__(self, other):
        if isinstance(other, FragmentContent):
            return self.tokens == other.tokens
        return False

    def __repr__(self):
        return "<" + str(self) + ">(" + str(self.tokens) + ")"

    def split_at_str(self, char: str):
        contents = [FragmentContent([])]
        for token in self.tokens:
            splits = token.raw.split(char)
            if len(splits[0]) > 0:
                contents[-1].tokens.append(token._replace(raw=splits[0]))
            for split in splits[1:]:
                if not split:
                    contents.append(FragmentContent([]))
                else:
                    contents.append(FragmentContent([token._replace(raw=split)]))
        return contents

    def split_at_pos(self, cutoff: int):
        """Split the list of tokens at position `cutoff`
        >>> Document.split_fragment_content([FragmentToken(raw='ab'), FragmentToken(raw='cx'), FragmentToken(raw='yz')], 3)
        ([FragmentToken(raw='ab'), FragmentToken(raw='c')],[FragmentToken(raw='x'), FragmentToken(raw='yz')])
        """
        if cutoff < 0:
            cutoff = self.__len__() + cutoff
        if cutoff >= len(self):
            return [self, FragmentContent([])]
        before = []
        after = []
        position = 0
        for token in self.tokens:
            if after:
                after.append(token)
                continue
            if len(token.raw) <= cutoff - position:
                before.append(token)
                position += len(token.raw)
                continue
            before.append(token._replace(raw=token.raw[:cutoff - position]))
            after.append(token._replace(raw=token.raw[cutoff - position:]))
        return [FragmentContent(before), FragmentContent(after)]

    def to_contents(self):
        return Contents(tokens=self.tokens)

    def endswith(self, string: str):
        return self.__str__().endswith(string)

    # Replacement for repl.sub()
    def re_sub(self, repl: re, sub_token: [FragmentToken] = []):
        """ Replacement method for repl.sub(str, str)

        Substitutes all matches in the str of this fragment with the sub_tokens.
        sub_tokens defaults to [] which equals removal of any match.

        Returns a FragmentContent with substitute tokens.
        """
        tokens = []
        last = FragmentContent(self.tokens)
        last_index = 0
        had_match = False
        for match in repl.finditer(str(self)):
            had_match = True
            first = last.split_at_pos(match.start() - last_index)[0]
            last = last.split_at_pos(match.end() - last_index)[-1]
            last_index = match.end()
            tokens += first.tokens + sub_token
        if had_match:
            tokens += last.tokens
            return FragmentContent(tokens)
        else:
            return self

    # Replacement for repl.match().groups()
    def re_match_groups(self, repl: re):
        """ Replacement method for repl.match(str).groups()

        Executes match over the whole str of the FragmentContent and returns
        the groups of this match as FragmentContent.

        Method returns (prefix, center, suffix).
        Returns None if there is no match.
        """
        match = repl.match(str(self))
        if match is None:
            return None
        first_split = self.split_at_pos(match.start())
        last_split = first_split[1].split_at_pos(match.end())
        prefix = first_split[0]
        center = last_split[0]
        suffix = last_split[1]
        return prefix, center, suffix

class Driver():
    def __init__(self):
        self.observer : Observer = StderrObserver()

    @classmethod
    def version_info(cls, binpath=None):
        raise NotImplementedError()

    def annotate(self, chunks):
        """Annotate multiple `chunks` of code.

        All fragments are executed in the same (fresh) prover instance.  The
        return value is a list with as many elements as in `chunks`, but each
        element is a list of fragments: either ``Text`` instances (whitespace
        and comments) or ``Sentence`` instances (code).
        """
        raise NotImplementedError()

class CLIDriver(Driver): # pylint: disable=abstract-method
    BIN: str = "unset-binary"
    NAME: str = "cli-driver"

    CLI_ARGS: Tuple[str, ...] = ()
    VERSION_ARGS = ("--version",)

    CLI_ENCODING = "utf-8"

    def __init__(self, args=(), fpath="-", binpath=None):
        super().__init__()
        self.fpath = Path(fpath)
        self.user_args = args
        self.binpath: str = binpath or self.BIN

    @classmethod
    def version_info(cls, binpath=None):
        bs = subprocess.check_output([cls.resolve_driver(binpath), *cls.VERSION_ARGS])
        return DriverInfo(cls.NAME, bs.decode('ascii', 'ignore').strip())

    @property
    def metadata(self):
        return {"args": self.user_args}

    @classmethod
    def driver_not_found(cls, binpath) -> NoReturn:
        """Raise an error to indicate that ``binpath`` cannot be found."""
        raise ValueError("{} binary not found (bin={}).".format(cls.NAME, binpath))

    @classmethod
    def resolve_driver(cls, binpath: str) -> str:
        assert cls.BIN
        path: Optional[str] = which(binpath or cls.BIN)
        if path is None:
            cls.driver_not_found(binpath)
        return path

    @staticmethod
    def _debug_start(cmd):
        debug(" ".join(quote(s) for s in cmd), '# ')

    @classmethod
    def _proc_out(cls, p):
        return p.stderr

    def run_cli(self, working_directory=None, capture_output=True, more_args=()):
        cmd = [self.resolve_driver(self.binpath),
               *self.CLI_ARGS, *self.user_args, *more_args]
        self._debug_start(cmd)
        p = subprocess.run(cmd, cwd=working_directory, capture_output=capture_output, check=False, encoding=self.CLI_ENCODING)
        if p.returncode != 0:
            MSG = "Driver {} ({}) exited with code {}:\n{}"
            raise ValueError(MSG.format(self.NAME, self.binpath, p.returncode,
                                        indent(self._proc_out(p), "   ")))
        return p.stdout

class REPLDriver(CLIDriver): # pylint: disable=abstract-method
    REPL_ARGS: Tuple[str, ...] = ()

    def __init__(self, args=(), fpath="-", binpath=None):
        super().__init__(args, fpath, binpath)
        self.repl = None
        self.instance_args: Tuple[str, ...] = ()

    def __enter__(self):
        self.reset()
        return self

    def __exit__(self, *_exn):
        self.kill()
        return False

    def _read(self):
        response = self.repl.stdout.readline()
        debug(response, '<< ')
        return response

    def _write(self, s, end):
        debug(s, '>> ')
        self.repl.stdin.write(s + end)  # type: ignore
        self.repl.stdin.flush()

    def kill(self):
        """Terminate this prover instance."""
        if self.repl:
            self.repl.kill()
            try:
                self.repl.stdin.close()
                self.repl.stdout.close()
            finally:
                self.repl.wait()

    def _start(self, stdin=PIPE, stderr=PIPE, stdout=PIPE, more_args=()):
        cmd = [self.resolve_driver(self.binpath),
               *self.REPL_ARGS, *self.user_args, *self.instance_args, *more_args]
        self._debug_start(cmd)
        # pylint: disable=consider-using-with
        return subprocess.Popen(cmd, stdin=stdin, stderr=stderr, stdout=stdout)

    def reset(self):
        """Start or restart this prover instance."""
        self.repl = self._start(stderr=None)

class TextREPLDriver(REPLDriver): # pylint: disable=abstract-method
    REPL_ENCODING = "utf-8"

    def _start(self, *args, **kwargs):
        repl = super()._start(*args, **kwargs)
        repl.stdin = TextIOWrapper( #type: ignore
            repl.stdin, write_through=True, encoding=self.REPL_ENCODING) #type: ignore
        repl.stdout = TextIOWrapper( #type: ignore
            repl.stdout, encoding=self.REPL_ENCODING) #type: ignore
        return repl

DRIVERS_BY_LANGUAGE = {
    "coq": {
        "sertop": (".serapi", "SerAPI"),
        "sertop_noexec": (".serapi", "SerAPI_noexec"),
        "coqc_time": (".coqc_time", "CoqcTime"),
    },
    "lean3": {
        "lean3_repl": (".lean3", "Lean3")
    },
    "lean4": {
        "leanInk": (".lean4", "Lean4")
    },
}

DEFAULT_DRIVERS = {lang: next(iter(drivers)) for lang, drivers in DRIVERS_BY_LANGUAGE.items()}
ALL_LANGUAGES = DEFAULT_DRIVERS.keys()
ALL_DRIVERS = {d for ds in DRIVERS_BY_LANGUAGE.values() for d in ds}

def resolve_driver(input_language, driver_name):
    if input_language not in DRIVERS_BY_LANGUAGE:
        raise ValueError("Unknown language: {}".format(input_language))
    known_drivers = DRIVERS_BY_LANGUAGE[input_language]
    if driver_name not in known_drivers:
        MSG = "Unknown driver for language {}: {}"
        raise ValueError(MSG.format(input_language, driver_name))
    mod, cls = known_drivers[driver_name]
    return getattr(import_module(mod, __package__), cls)
