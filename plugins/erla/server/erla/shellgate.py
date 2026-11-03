"""Shell and browser coverage for the PreToolUse gate (G1.1, G2.1-G2.3, G7.1). Standard library only.

The web gate (``erla.webgate``) once inspected only WebSearch and WebFetch, so ``curl``, ``Invoke-WebRequest``,
a Python one-liner or a browser MCP tool sent a restricted name to the web unchecked, and a shell command could
write into a library root, the site config (``web.allow_terms``) or a protocol file. This module decides, by
parsing alone, what such a call needs:

* ``scan(command)`` - does a shell command have a network verb or URL, and may it write anything at all? A
  command with neither passes at once (the fast path; most shell calls).
* ``network_text(command)`` - what a network command could send: the command, its de-quoted form and any
  ``-EncodedCommand`` payload. ``erla.webgate`` applies the WebSearch / WebFetch check to it.
* ``analyse(command, cwd)`` then ``write_reason(analysis, protected)`` - the paths the command writes, and the
  deny reason when one is inside a protected path (or the command changes the web allowlist).
* ``mcp_text(tool, input)`` - what an MCP tool call could send to a site (browser tools: every string; any
  other tool: its strings when one is a URL).

Plugin hooks run on every shell call of every session that has ERLA enabled, so the plugin's hook launcher
(``scripts/webgate.py``, under uv's Python, without ERLA's runtime) loads this file by path and decides most
calls itself; only a network command, or a write when the protected-path list is not cached, starts ERLA's
runtime. Nothing here may raise on odd input (empty, very long, non-UTF-8, multi-line): callers turn an
exception into a deny, which on this path would block ordinary shell calls.

Known limits (documented, not silent): a target built at run time from parts this parser cannot see (a variable
set by an earlier call, a path read from a file, a script file's own code) is not resolved; such a write is
denied only when the command text names a protected path. Obfuscated verbs beyond quote and escape splitting
(``$c='cu'+'rl'; & $c``) are not recognised as network commands; their text is still checked whenever it
carries a URL.
"""

from __future__ import annotations

import base64
import binascii
import os
import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import cast

SHELL_TOOLS = ("Bash", "PowerShell")
KNOWN_TOOLS = ("WebSearch", "WebFetch", *SHELL_TOOLS)   # tools fast_allow judges by name; any other passes
ERLA_MCP = re.compile(r"^mcp__(?:remote-devices__)?plugin_erla_erla__")
MAX_SCAN = 1_000_000            # characters; a longer command is checked in full by the runtime

# ------------------------------------------------------------------ network verbs
_NET_WORDS = (
    "curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod", "start-bitstransfer", "bitsadmin",
    "certutil", "webclient", "httpclient", "webrequest", "downloadstring", "downloadfile", "uploadstring",
    "uploadfile", "urllib", "urlopen", "requests", "httpx", "aiohttp", "http.client", "socket", "tcpclient",
    "udpclient", "net.sockets", "nc", "ncat", "netcat", "telnet", "ftp", "tftp", "sftp", "scp", "gh", "fetch",
    "xmlhttprequest", "send-mailmessage", "smtplib", "smtpclient", "nslookup", "resolve-dnsname", "dig",
    "ping", "test-netconnection", "tnc", "msedge", "chrome", "firefox", "lynx", "aria2c", "httpie", "xh",
)
NET_RE = re.compile(r"://|\bwww\.|\bmailto:|(?<![\w-])(?:" + "|".join(re.escape(w) for w in _NET_WORDS)
                    + r")(?![\w-])", re.I)
ENCODED_RE = re.compile(r"(?<![\w-])-(?:e|ec|enc|encodedcommand)\s+['\"]?([A-Za-z0-9+/=]{8,})", re.I)

# ------------------------------------------------------------------ writes
_WRITE_VERBS = {
    # every non-option argument is written (created, changed, moved away or removed)
    "rm", "del", "erase", "rd", "rmdir", "mkdir", "md", "touch", "truncate", "remove-item", "ri", "new-item",
    "ni", "clear-content", "clc", "mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni", "tee",
    "tee-object", "unlink", "shred", "ln", "mklink", "set-content", "sc", "add-content", "ac", "out-file",
    "export-csv", "epcsv", "export-clixml", "set-item", "si", "chmod", "chown", "icacls", "attrib", "sed",
    "perl", "dd", "new-itemproperty", "set-itemproperty",
}
# only the destination is written; the sources are read
_COPY_VERBS = {"cp", "copy", "copy-item", "cpi", "xcopy", "robocopy", "rsync", "install", "tar", "unzip",
               "expand-archive", "7z", "7za", "compress-archive"}
_DEST_OPTS = {"-t", "--target-directory", "-destination", "-destinationpath", "-d", "-c", "--directory", "-f",
              "--file", "-literalpath", "-path"}
_PATH_OPTS = {"-path", "-literalpath", "-filepath", "-destination", "-destinationpath", "-target", "-value"}
_CD_VERBS = {"cd", "chdir", "set-location", "sl", "pushd", "push-location"}
_PREFIX_WORDS = {"sudo", "command", "builtin", "exec", "nohup", "time", "env", "nice", "&", ".", "call", "xargs",
                 "doas", "then", "do", "else", "!", "{", "}"}
_SHELLS_C = {"bash", "sh", "zsh", "dash", "ksh"}
_PS = {"powershell", "pwsh"}
_RUNNERS = {"start-process", "saps", "start", "invoke-command", "icm", "iex", "invoke-expression"}
_INTERPRETERS = {"python", "python3", "py", "pythonw", "node", "deno", "bun", "perl", "ruby", "php", "lua",
                 "osascript", "cscript", "wscript", "mshta"}
_CMD_SWITCH_VERBS = {"del", "erase", "rd", "rmdir", "md", "mkdir", "move", "ren", "rename", "mklink", "icacls",
                     "attrib", "copy", "xcopy", "robocopy"}


def _words_re(words: Iterable[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


WRITE_RE = re.compile(r"(?<![\w$.-])(?:" + _words_re(_WRITE_VERBS | _COPY_VERBS) + r")(?![\w-])", re.I)
CODE_RE = re.compile(r"(?<![\w.-])(?:" + _words_re(_INTERPRETERS | _SHELLS_C | _PS | _RUNNERS | {"cmd", "eval"})
                     + r")(?:\d[\d.]*)?(?:\.exe)?(?![\w-])", re.I)
# a redirection that writes a file: not 2>&1, >&2, >/dev/null, >$null, > nul, =>, ->, >=
_REDIR_RE = re.compile(r"(?<![=\->])>>?(?![=&>])(?!\s*(?:/dev/null|\$null|nul)\b)", re.I)
# writes inside interpreter code (python -c, node -e, a here-document fed to python)
CODE_WRITE_RE = re.compile(
    r"open\s*\([^)]*?['\"][rbtU]*[wax+][rbt+]*['\"]|\.write_(?:text|bytes)\b|\.(?:unlink|rmdir|mkdir|touch|"
    r"rename|replace|write|writelines|truncate|symlink_to|hardlink_to)\s*\(|\b(?:shutil|os)\s*\.\s*(?:copy\w*|move|"
    r"rename|replace|remove|unlink|rmtree|rmdir|mkdir|makedirs|truncate|system|popen|spawn\w*|exec\w*)\b|"
    r"\bsubprocess\b|\bfs\s*\.\s*\w*(?:write|append|copy|rename|unlink|rm|mkdir)\w*|\b(?:to_csv|to_excel|to_json|"
    r"to_parquet|savefig|save|dump)\s*\(|\[(?:system\.)?io\.(?:file|directory)\]::\w*(?:write|append|copy|move|"
    r"delete|create|replace)|\b(?:writefile|appendfile|createtextfile|copyfile|movefile|deletefile)\b",
    re.I)
WEB_ALLOW_RE = re.compile(r"\bweb-allow\b|\bset_web_allow_terms\b", re.I)
_WEB_ALLOW_CALL = re.compile(r"(?<![\w-])erla(?:\.exe)?(?:\.cli)?\s+(?:[^|;&\n]*\s)?web-allow\b|"
                             r"\bset_web_allow_terms\b", re.I)


def _dequote(text: str) -> str:
    """The text with quote, escape and caret characters removed, as a shell reads ``c''url`` or ``cu\\rl``."""
    return re.sub(r"[\"'`^\\]", "", text)


def _encoded_payloads(command: str) -> list[str | None]:
    """PowerShell ``-EncodedCommand`` payloads (base64 of UTF-16LE), decoded; None for one that does not decode."""
    out: list[str | None] = []
    for m in ENCODED_RE.finditer(command):
        b64 = m.group(1)
        try:
            out.append(base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-le"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            out.append(None)
    return out


def scan(command: object) -> tuple[bool, bool]:
    """(network, writes) for a shell command, by pattern alone. ``writes`` is True for any command that may
    write a file or run code that could (redirection, a write verb, shell or interpreter code, web-allow); which
    paths, and whether one is protected, comes after (``analyse``, ``write_reason``). A value that is not text,
    or text too long to scan, counts as both (the runtime checks it in full)."""
    if not isinstance(command, str) or len(command) > MAX_SCAN:
        return True, True
    texts = [command, _dequote(command), *[p for p in _encoded_payloads(command) if p]]
    net = any(NET_RE.search(t) for t in texts)
    writes = bool(_REDIR_RE.search(command) or ENCODED_RE.search(command)
                  or any(WRITE_RE.search(t) or CODE_RE.search(t) or WEB_ALLOW_RE.search(t) for t in texts[1:]))
    return net, writes


def network_text(command: str) -> str:
    """What a network command could send to a site, for the G2 check: the command as written, the command with
    quotes and escapes removed (``'kes''trel'``), and decoded ``-EncodedCommand`` payloads."""
    parts = [command, _dequote(command), *[p for p in _encoded_payloads(command) if p]]
    return "\n".join(dict.fromkeys(parts))


# ------------------------------------------------------------------ MCP tools
_EGRESS_NAME = re.compile(r"browser|chrome|playwright|puppeteer|navigate|fetch|http|web|url|scrape|crawl|download|"
                          r"upload|request|search|curl|javascript|script|eval|type|form|clipboard|computer|tab",
                          re.I)
_SHELL_NAME = re.compile(r"powershell|bash|shell|terminal|cmd|command|exec", re.I)
_URL_RE = re.compile(r"://|\bwww\.", re.I)


def _strings(value: object, depth: int = 0) -> Iterable[str]:
    if depth > 20:
        yield "(nested too deep)"
    elif isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():                      # pyright: ignore[reportUnknownVariableType]
            yield str(k)                                # pyright: ignore[reportUnknownArgumentType]
            yield from _strings(v, depth + 1)           # pyright: ignore[reportUnknownArgumentType]
    elif isinstance(value, (list, tuple)):
        for v in value:                                 # pyright: ignore[reportUnknownVariableType]
            yield from _strings(v, depth + 1)           # pyright: ignore[reportUnknownArgumentType]
    elif value is not None:
        yield str(value)


def mcp_command(tool: str, tool_input: object) -> str | None:
    """The shell command of an MCP tool that runs one (a terminal or PowerShell server), else None."""
    if not isinstance(tool_input, dict) or not _SHELL_NAME.search(tool.split("__")[-1]):
        return None
    inp = cast(dict[str, object], tool_input)
    for key in ("command", "cmd", "script", "commands"):
        v = inp.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return "\n".join(str(x) for x in cast(list[object], v))
    return None


def mcp_text(tool: str, tool_input: object) -> str | None:
    """What an MCP tool call could send to a site, or None when it sends nothing the gate checks. ERLA's own
    tools: None. A browser or fetch-like tool (by server or tool name): every string of its input (a typed
    search, a URL, a script). Any other tool: its strings when one of them is a URL."""
    if ERLA_MCP.search(tool):
        return None
    texts = list(_strings(tool_input))
    parts = tool.split("__")
    if any(_EGRESS_NAME.search(p) for p in parts[1:]):
        return "\n".join(texts)
    if any(_URL_RE.search(t) for t in texts):
        return "\n".join(texts)
    return None


def fast_allow(tool: object, tool_input: object) -> bool:
    """True when the gate surely has nothing to check in this call: no network verb or URL, nothing that may
    write, not a web tool. Pure; never raises for any JSON value. ``erla.webgate.decide`` starts with this same
    test, so the launcher's fast path and the runtime agree by construction."""
    if not isinstance(tool, str):
        return False
    if tool in ("WebSearch", "WebFetch"):
        return False
    if tool in SHELL_TOOLS:
        net, writes = scan(tool_input.get("command") if isinstance(tool_input, dict) else None)  # pyright: ignore
        return not (net or writes)
    if tool.startswith("mcp__"):
        if ERLA_MCP.search(tool):
            return True
        cmd = mcp_command(tool, tool_input)
        if cmd is not None and any(scan(cmd)):
            return False
        return mcp_text(tool, tool_input) is None
    return True


def shell_command(tool: str, tool_input: object) -> tuple[str, bool] | None:
    """(command, PowerShell quoting) for a call that runs a shell command, else None. A shell tool without a
    text ``command`` raises KeyError (schema drift): the caller denies."""
    if tool in SHELL_TOOLS:
        cmd = tool_input.get("command") if isinstance(tool_input, dict) else None   # pyright: ignore
        if not isinstance(cmd, str):
            raise KeyError("command")
        return cmd, tool == "PowerShell"
    if tool.startswith("mcp__") and not ERLA_MCP.search(tool):
        cmd = mcp_command(tool, tool_input)
        if cmd is not None:
            return cmd, "powershell" in tool.lower()
    return None


# ------------------------------------------------------------------ parsing shell commands into words
def _split(command: str, ps: bool) -> list[list[str]]:
    """Simple commands of a shell line as word lists. Quotes are removed; backslashes are kept (Windows paths);
    ``;``, newlines, ``|``, ``&``, parentheses and (bash) backticks end a command; a redirection becomes the
    word ``">"`` followed by its target. ``$(...)`` inside double quotes is handled by ``_subcommands``."""
    segs: list[list[str]] = []
    cur: list[str] = []
    tok: list[str] = []
    started = False
    quote = ""
    i, n = 0, len(command)

    def end_tok() -> None:
        nonlocal started
        if started:
            cur.append("".join(tok))
        tok.clear()
        started = False

    def end_seg() -> None:
        end_tok()
        if cur:
            segs.append(cur.copy())
        cur.clear()

    while i < n:
        c = command[i]
        if quote == "'":
            if c == "'":
                if ps and i + 1 < n and command[i + 1] == "'":
                    tok.append("'")
                    i += 1
                else:
                    quote = ""
            else:
                tok.append(c)
        elif quote == '"':
            if c == '"':
                quote = ""
            elif c == "`" and ps and i + 1 < n:
                tok.append(command[i + 1])
                i += 1
            elif c == "\\" and not ps and i + 1 < n and command[i + 1] in '"$`\\':
                tok.append(command[i + 1])
                i += 1
            else:
                tok.append(c)
        elif c in "'\"":
            quote = c
            started = True
        elif c in " \t\r":
            end_tok()
        elif c == "{" and tok and tok[-1] == "$" or c == "}" and "${" in "".join(tok).rsplit("}", 1)[-1]:
            tok.append(c)                               # ${NAME}, ${env:NAME}
            started = True
        elif c in ";\n|&(){}" or (c == "`" and not ps):
            end_seg()
        elif c == "`":                                  # PowerShell escape outside quotes
            if i + 1 < n:
                tok.append(command[i + 1])
                started = True
                i += 1
        elif c == "\\" and not ps and i + 1 < n and command[i + 1] in " '\"\n":
            tok.append(command[i + 1])
            started = True
            i += 1
        elif c == ">":
            if started and not re.fullmatch(r"\d*|\*", "".join(tok)):
                end_tok()
            else:                                       # 2> and *> : the descriptor is not a word
                tok.clear()
                started = False
            j = i + 1
            if j < n and command[j] == ">":
                j += 1
            if j < n and command[j] == "&":
                j += 1
                if j < n and (command[j].isdigit() or command[j] == "-"):
                    i = j + 1                            # 2>&1, >&-: a descriptor, not a file
                    continue
            cur.append(">")
            i = j
            continue
        elif c == "<":
            end_tok()
            cur.append("<")
        else:
            tok.append(c)
            started = True
        i += 1
    end_seg()
    return segs


def _subcommands(command: str) -> list[str]:
    """Command substitutions ``$(...)`` anywhere, also inside double quotes (they run there too)."""
    return [m.group(1) for m in re.finditer(r"\$\(([^()]*)\)", command)]


# ------------------------------------------------------------------ resolving targets
_DRIVE_RE = re.compile(r"^/(?:cygdrive/|mnt/)?([a-zA-Z])(?=/|$)")
_NULL = {"/dev/null", "$null", "nul", "/dev/stdout", "/dev/stderr", "con"}


def _expand(word: str, env: Mapping[str, str], variables: Mapping[str, str], cwd: str | None) -> str | None:
    """Variables and ``~`` expanded; None when a variable is unknown."""
    lenv = {k.lower(): v for k, v in env.items()}
    lvars = {k.lower(): v for k, v in variables.items()}
    home = lenv.get("userprofile") or lenv.get("home") or os.path.expanduser("~")
    special = {"home": home, "pwd": cwd}
    unknown = False

    def sub(m: re.Match[str]) -> str:
        nonlocal unknown
        name = next(g for g in m.groups() if g is not None).lower()
        v = lenv.get(name[4:]) if name.startswith("env:") else lvars.get(name, lenv.get(name, special.get(name)))
        if v is None:
            unknown = True
            return ""
        return v

    out = re.sub(r"\$\{([^}]+)\}|\$((?:env:)?[A-Za-z_]\w*)|%([A-Za-z_][\w()]*)%", sub, word)
    if unknown or "$" in out:
        return None
    if out == "~" or out.startswith(("~/", "~\\")):
        out = home + out[1:]
    return out


def _resolve(word: str, cwd: str | None, env: Mapping[str, str], variables: Mapping[str, str],
             bash: bool) -> list[str] | None:
    """The absolute paths a target word may name (as written and, in bash, with backslash escapes removed), or
    None when it cannot be resolved (an unknown variable, a relative path with an unknown working folder)."""
    if word in ("", "-", ">", "<") or word.lower() in _NULL:
        return []
    expanded = _expand(word, env, variables, cwd)
    if expanded is None:
        return None
    forms = [expanded]
    if bash and "\\" in expanded:
        forms.append(re.sub(r"\\(.)", r"\1", expanded))
    out: list[str] = []
    for f in forms:
        p = f.strip()
        if p.lower().startswith(("\\\\?\\", "//?/")):
            p = p[4:]
        if os.name == "nt":
            p = _DRIVE_RE.sub(lambda m: m.group(1) + ":", p.replace("\\", "/"))
        got = _place(p, cwd)
        if got is None:
            return None
        out += got
    return list(dict.fromkeys(out))


_WIN_ABS = re.compile(r"^[a-zA-Z]:/")


def _win_key(p: str) -> str:
    """A Windows drive path compared on another OS: forward slashes, ``..`` resolved, lower case (normcase on
    Windows folds case the same way)."""
    return posixpath.normpath(p.replace("\\", "/")).lower()


def _place(p: str, cwd: str | None) -> list[str] | None:
    """The absolute forms of one written path, or None when it is relative and the working folder is unknown.

    Windows path forms (backslash separators, drive letters, Git Bash and WSL drive paths such as ``/c/lib``) are
    read the same way on every OS: the commands run on the user's Windows computer, and the decision must not
    depend on the OS the gate's code runs on (CI runs it on Linux, where ``$d\\Methods`` and ``/c/lib`` were
    once allowed, F-04). Off Windows they are added to the path as written, never in place of it, so they only
    ever deny more."""
    wild = re.search(r"[*?\[]", p)
    if wild:                                            # a glob: the folder it writes into
        p = re.sub(r"[/\\][^/\\]*$", "", p[:wild.start()]) or "/"
    if not p:
        return []
    if re.match(r"^[a-zA-Z]:(?![/\\])", p):            # "C:foo": relative to C:'s folder
        if cwd is None:
            return None
        p = os.path.join(cwd, p[2:])
    elif not (os.path.isabs(p) or _WIN_ABS.match(p.replace("\\", "/"))):
        if cwd is None:
            return None
        p = os.path.join(cwd, p)
    try:
        if os.name == "nt":
            return [os.path.normcase(os.path.abspath(p)), os.path.normcase(os.path.realpath(p))]
        out: list[str] = []
        win = _DRIVE_RE.sub(lambda m: m.group(1) + ":", p.replace("\\", "/"))
        if _WIN_ABS.match(win):
            out.append(_win_key(win))
        if not _WIN_ABS.match(p.replace("\\", "/")):        # a POSIX path: as written, and with \ as separator
            for q in dict.fromkeys([p, p.replace("\\", "/")]):
                out += [os.path.normcase(os.path.abspath(q)), os.path.normcase(os.path.realpath(q))]
        return out
    except (OSError, ValueError):
        return None


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


class Protected:
    """Folders and files no shell command may write: (kind, path) pairs, normalised once."""

    def __init__(self, items: Iterable[tuple[str, str]]):
        self.items: list[tuple[str, str]] = []
        for kind, p in items:
            if not p:
                continue
            forms = ([_win_key(p)] if os.name != "nt" and _WIN_ABS.match(p.replace("\\", "/")) else
                     [os.path.normcase(os.path.abspath(p)), os.path.normcase(os.path.realpath(p))])
            for form in dict.fromkeys(forms):
                self.items.append((kind, form))
        self.markers: list[tuple[str, str]] = []
        for kind, p in self.items:
            q = p.replace("\\", "/").casefold()
            parts = [x for x in q.split("/") if x]
            self.markers.append((kind, q))
            if re.match(r"^[a-z]:/", q):
                self.markers.append((kind, "/" + q[0] + q[2:]))
            if len(parts) >= 3:
                self.markers.append((kind, "/".join(parts[-2:])))
            if parts and parts[-1] == "config":
                self.markers.append((kind, "library_config"))

    def hit(self, paths: Iterable[str]) -> tuple[str, str] | None:
        for p in paths:
            for kind, root in self.items:
                if _inside(p, root):
                    return kind, root
        return None

    def mentioned(self, text: str) -> str | None:
        t = text.replace("\\", "/").casefold()
        forms = {re.sub(r"/+", "/", t), re.sub(r"/+", "/", _dequote(t))}
        for kind, m in self.markers:
            if any(m in f for f in forms):
                return kind
        return None


class Analysis:
    """What a shell command writes: resolved target paths, whether some target could not be resolved, and a
    refusal that needs no path list (web-allow, an undecodable encoded command). A plain class: importing
    dataclasses costs the hook launcher about 10 ms on every shell call."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.unresolved = False
        self.refusal: str | None = None

    @property
    def empty(self) -> bool:
        return not self.paths and not self.unresolved and self.refusal is None


def _strip_options(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _command(words: list[str], ps: bool) -> tuple[str, list[str], list[tuple[str, bool]]]:
    """(verb, written targets, inline code) of one simple command. Inline code is shell code to analyse
    further, with its quoting (``bash -c``, ``powershell -Command``, ``cmd /c``, ``Start-Process``)."""
    targets: list[str] = []
    rest: list[str] = []
    k = 0
    while k < len(words):
        if words[k] in (">", "<"):
            if words[k] == ">" and k + 1 < len(words):
                targets.append(words[k + 1])
            k += 2
            continue
        rest.append(words[k])
        k += 1
    w = rest
    while w and (w[0].lower() in _PREFIX_WORDS or re.fullmatch(r"[A-Za-z_]\w*=.*", w[0])):
        w = w[1:]
        while w and w[0].startswith("-"):              # sudo -u x, env -i, xargs -I{}
            w = w[1:]
    if not w:
        return "", targets, []
    verb = re.sub(r"\.(?:exe|cmd|bat|com|ps1)$", "", re.split(r"[\\/]", w[0])[-1].lower())
    args = w[1:]
    low = [a.lower() for a in args]
    code: list[tuple[str, bool]] = []
    if verb in _PS:
        i = 0
        while i < len(args) and args[i].startswith("-"):
            o = low[i]
            if o in ("-command", "-c"):
                i += 1
                break
            i += 2 if o in ("-executionpolicy", "-ep", "-ex", "-windowstyle", "-w", "-file", "-f", "-workingdirectory",
                            "-wd", "-outputformat", "-of", "-inputformat", "-if", "-version", "-v",
                            "-encodedcommand", "-enc", "-ec", "-e", "-configurationname") else 1
        code.append((" ".join(args[i:]), True))
    elif verb in _SHELLS_C:
        if "-c" in low:
            code.append((" ".join(args[low.index("-c") + 1:]), False))
    elif verb == "cmd":
        cut = next((i for i, a in enumerate(low) if a in ("/c", "/k")), None)
        if cut is not None:
            code.append((" ".join(args[cut + 1:]), True))
    elif verb in _RUNNERS:
        code.append((" ".join(_strip_options(args)), True))
    elif verb in _WRITE_VERBS:
        if verb in ("sed", "perl") and not any(a == "-i" or a.startswith(("-i", "--in-place")) for a in low):
            return verb, targets, code
        take = False
        for a, la in zip(args, low):
            if take:
                targets.append(a)
                take = False
            elif la in _PATH_OPTS:
                take = True
            elif la.startswith("-") or (verb in _CMD_SWITCH_VERBS and re.fullmatch(r"/[a-z?]{1,4}", la)):
                continue
            else:
                targets.append(a)
    elif verb in _COPY_VERBS:
        positional: list[str] = []
        take = False
        for a, la in zip(args, low):
            if take:
                targets.append(a)
                take = False
            elif "=" in la and la.split("=", 1)[0] in ("--target-directory", "--directory", "--file"):
                targets.append(a.split("=", 1)[1])
            elif verb in ("7z", "7za") and la.startswith("-o") and len(la) > 2:
                targets.append(a[2:])
            elif la in _DEST_OPTS:
                take = True
            elif la.startswith("-") or (verb in _CMD_SWITCH_VERBS and re.fullmatch(r"/[a-z?]{1,4}(?::\S*)?", la)):
                continue
            else:
                positional.append(a)
        if verb == "robocopy" and len(positional) >= 2:
            targets.append(positional[1])
        elif verb in ("tar", "unzip", "7z", "7za"):
            pass                                        # the destination comes from -C / -d / -o above
        elif positional:
            targets.append(positional[-1])
    if verb == "dd":
        targets += [a[3:] for a in args if a.lower().startswith("of=")]
    return verb, targets, code


def analyse(command: str, cwd: str | None, env: Mapping[str, str] | None = None, ps: bool = False,
            depth: int = 0) -> Analysis:
    """The paths a shell command writes. ``ps``: PowerShell quoting (backtick escapes) instead of bash
    (backslash escapes). ``cwd``: the folder the command starts in (the hook's ``cwd``), None if unknown."""
    env = dict(os.environ) if env is None else env
    res = Analysis()
    if depth > 6:
        res.refusal = "an inline command nested too deep to check"
        return res
    if _WEB_ALLOW_CALL.search(_dequote(command)):
        res.refusal = "web-allow"
        return res

    def merge(sub: Analysis) -> bool:
        res.paths += sub.paths
        res.unresolved |= sub.unresolved
        res.refusal = res.refusal or sub.refusal
        return res.refusal is not None

    for payload in _encoded_payloads(command):
        if payload is None:
            res.refusal = "an encoded command that could not be decoded"
            return res
        if merge(analyse(payload, cwd, env, True, depth + 1)):
            return res
    for sub in _subcommands(command):
        if merge(analyse(sub, cwd, env, ps, depth + 1)):
            return res
    variables: dict[str, str] = {}
    interp = False
    for words in _split(command, ps):
        m = re.fullmatch(r"\$([A-Za-z_]\w*)=(.*)", words[0])
        if len(words) >= 3 and words[1] == "=" and words[0].startswith("$"):      # PowerShell $x = "value"
            variables[words[0][1:]] = " ".join(words[2:])
            words = words[2:]
        elif m:
            variables[m.group(1)] = " ".join([m.group(2), *words[1:]])
            continue
        elif len(words) == 1 and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]):     # bash x=value
            name, _, value = words[0].partition("=")
            variables[name] = value
            continue
        verb, targets, code = _command(words, ps)
        if verb in _CD_VERBS:
            dest = next((a for a in words[1:] if not a.startswith("-") and a.lower() != "/d"), "~")
            where = _resolve(dest, cwd, env, variables, not ps)
            cwd = where[0] if where else None
            continue
        for text, cps in code:
            interp = True
            if merge(analyse(text, cwd, env, cps, depth + 1)):
                return res
        if verb in _INTERPRETERS or re.fullmatch(r"python\d[\d.]*", verb) or verb == "eval":
            interp = True
        for t in targets:
            paths = _resolve(t, cwd, env, variables, not ps)
            if paths is None:
                res.unresolved = True
            else:
                res.paths += paths
    if interp and CODE_WRITE_RE.search(command):
        # interpreter code that writes: every quoted literal may be the file it writes
        for lit in re.finditer(r"""(['"])((?:(?!\1)[^\n]){2,})\1""", command):
            paths = _resolve(lit.group(2), cwd, env, variables, False)
            res.paths += paths or []
        res.unresolved = True
    res.paths = list(dict.fromkeys(res.paths))
    return res


def write_reason(analysis: Analysis, protected: Protected, command: str) -> str | None:
    """The deny reason when the analysed command writes into a protected path, changes the web allowlist,
    or writes somewhere unresolved while naming a protected path; else None."""
    if analysis.refusal == "web-allow":
        return ("ERLA write guard (G2): this command was not run because it changes the web gate's allowlist "
                "(web.allow_terms). That is the user's decision: ask the user to run `erla web-allow` in their own "
                "terminal if they want the term allowed.")
    if analysis.refusal:
        return _reason(analysis.refusal, None)
    hit = protected.hit(analysis.paths)
    if hit:
        return _reason(hit[0], hit[1])
    if analysis.unresolved:
        kind = protected.mentioned(command)
        if kind:
            return _reason(kind, None)
    return None


def _reason(kind: str, where: str | None) -> str:
    place = f" ({where})" if where else ""
    return (f"ERLA write guard (G1.1): this command was not run because it writes into {kind}{place}, or the "
            "guard could not tell that it does not. Library roots are read-only; the site config changes only "
            "through ERLA's own commands run by the user (web.allow_terms only with `erla web-allow`); protocol "
            "files change only with the user's approval of each change. Write results to the site's out/ folder "
            "instead, or ask the user.")
