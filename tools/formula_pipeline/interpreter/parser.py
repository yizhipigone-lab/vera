# -*- coding: utf-8 -*-
"""通达信公式词法/语法解析器 (2026-08-26, 全新编写)。

将公式源码解析为语句列表 AST:
  Assign(name, expr)   := 赋值 (中间变量)
  Output(name, expr)    : 具名输出
  Bare(expr)            行尾裸表达式 (匿名输出)
  DrawStmt(name)        画图语句 (跳过, 不参与信号)
表达式节点:
  Num(v) / Var(name) / Bin(op,l,r) / Cmp(op,l,r) / Logic('AND'|'OR',l,r)
  Not(e) / Neg(e) / Call(name, args)

语法要点 (对齐 TDX):
- 语句以 `;` 或换行分隔; `NAME:=expr` 赋值, `NAME:expr` 输出
- 输出可带 `,COLORRED`/`,LINETHICK2`/`,NODRAW` 等修饰 → 剥离
- 注释: `{...}` 与 `//...` 剥离
- 运算优先级: OR < AND < 比较 < 加减 < 乘除/模 < 一元 < 幂 < 原子
- AND/OR/NOT 既可作运算符也可作函数 AND(a,b)
"""
import re

# ---------------------------------------------------------------- 词法

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>\{[^}]*\}|//[^\n]*)
  | (?P<str>'[^']*'|"[^"]*")
  | (?P<num>\d+\.\d+|\.\d+|\d+\.|\d+)
  | (?P<name>[A-Za-z_\u4e00-\u9fa5①-⑩\uFF10-\uFF19][A-Za-z0-9_\u4e00-\u9fa5①-⑩\uFF10-\uFF19]*)
  | (?P<op>:=|<=|>=|<>|!=|==|=|>|<|\+|-|\*|/|\(|\)|,|;|\^|:|&&|\|\||&|\||!|\.)
""", re.VERBOSE)


def preprocess(source: str) -> str:
    """语句边界修复: 行尾无 `;` 且括号已闭合、下一行是新语句开头时补 `;`。

    TDX 导出绝大多数带分号, 此为兜底; 保守判定避免切断合法续行。
    """
    out_lines = []
    lines = source.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        out_lines.append(line)
        if not stripped or stripped.startswith("//") or stripped.startswith("{"):
            continue
        if stripped.endswith(";") or stripped.endswith(","):
            continue  # 已有分隔 / 修饰续行
        depth = 0
        for ch in stripped:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        if depth != 0:
            continue  # 跨行调用, 不切
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if not nxt:
            continue
        # 保守: 仅当下一行明确是新语句 (画图 / NAME:= / NAME:) 才补分号。
        # 宁可漏补 (留待语法错误暴露), 不可误切 (语义错误更隐蔽)。
        if (nxt.upper().startswith(tuple(DRAW_STMT_HEADS))
                or re.match(r"^[A-Za-z_\u4e00-\u9fa5]\w*\s*(:=|:)", nxt)):
            out_lines[-1] = line + ";"
    return "\n".join(out_lines)


def _unused():
    pass


def tokenize(src: str) -> list:
    """词法切分 → [(kind, value, pos)]; 未知字符抛 SyntaxError。"""
    tokens, pos = [], 0
    n = len(src)
    while pos < n:
        m = _TOKEN_RE.match(src, pos)
        if not m:
            raise SyntaxError(f"非法字符 {src[pos]!r} @ {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws",):
            continue
        if kind == "comment":
            # // 注释吃到行尾 (regex 已处理); {} 块注释
            continue
        tokens.append((kind, m.group()))
    return tokens


# ---------------------------------------------------------------- AST

class Node:
    def __repr__(self):
        return f"{type(self).__name__}({self})"


class Num(Node):
    def __init__(self, v):
        self.v = float(v)

    def __str__(self):
        return str(self.v)


class Var(Node):
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


class Bin(Node):
    def __init__(self, op, l, r):
        self.op, self.l, self.r = op, l, r

    def __str__(self):
        return f"{self.l} {self.op} {self.r}"


class Cmp(Node):
    def __init__(self, op, l, r):
        self.op, self.l, self.r = op, l, r

    def __str__(self):
        return f"{self.l} {self.op} {self.r}"


class Logic(Node):
    def __init__(self, op, l, r):
        self.op, self.l, self.r = op, l, r

    def __str__(self):
        return f"{self.l} {self.op} {self.r}"


class Not(Node):
    def __init__(self, e):
        self.e = e

    def __str__(self):
        return f"NOT {self.e}"


class Neg(Node):
    def __init__(self, e):
        self.e = e

    def __str__(self):
        return f"-{self.e}"


class Call(Node):
    def __init__(self, name, args):
        self.name, self.args = name, args

    def __str__(self):
        return f"{self.name}({', '.join(map(str, self.args))})"


# 语句
class Assign(Node):
    def __init__(self, name, expr):
        self.name, self.expr = name, expr

    def __str__(self):
        return f"{self.name} := {self.expr}"


class Output(Node):
    def __init__(self, name, expr):
        self.name, self.expr = name, expr

    def __str__(self):
        return f"{self.name} : {self.expr}"


class Bare(Node):
    def __init__(self, expr):
        self.expr = expr

    def __str__(self):
        return str(self.expr)


class DrawStmt(Node):
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"{self.name}(...)"


# ---------------------------------------------------------------- 语法

DRAW_STMT_HEADS = {
    "STICKLINE", "DRAWTEXT", "DRAWICON", "DRAWNUMBER", "DRAWSTRING",
    "DRAWGBK", "DRAWBAND", "DRAWKLINE", "PARTLINE", "VERTLINE", "HLINE",
    "PLOYLINE", "POLYLINE", "DRAWLINE", "DRAWTEXT_FIX", "DRAWNUMBER_FIX",
    "FILLRGN", "DRAWLINE_EX", "DRAWKLINE1", "SUPERSTR",
}

# 输出修饰 (跟在输出语句表达式后的逗号形式)
_DECOR = re.compile(r",\s*(COLOR\w+|LINETHICK\d|POINTDOT|NODRAW|CROSSDOT|"
                    r"VOLSTICK|COLORSTICK|DASHLINE|DOTLINE|SOLID|LAYER\d+|"
                    r"RGB\(.*?\)|ALIGN\d|VALIGN\d|LINESTICK|STICK)$",
                    re.IGNORECASE)


class Parser:
    """递归下降解析器。parse() → (stmts, undefined_names)。"""

    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0
        self.defined = set()   # 已赋值变量 (含输出)
        self.undefined = []     # 未定义标识符 (按出现序, 视为参数)

    # ---- token 辅助
    def peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else (None, None)

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def expect(self, val):
        k, v = self.next()
        if v != val:
            raise SyntaxError(f"期望 {val!r}, 得到 {v!r}")

    # ---- 语句层
    def parse(self):
        stmts = []
        while self.peek()[0] is not None:
            if self.peek()[1] == ";":
                self.next()
                continue
            stmts.append(self.statement())
        return stmts

    def statement(self):
        kind, val = self.peek()
        if kind == "name":
            # 画图语句 → 整句跳过 (直到顶层 ; 或 EOF)
            if val.upper() in DRAW_STMT_HEADS:
                self._skip_draw()
                return DrawStmt(val.upper())
            # NAME := / NAME : / 纯表达式开头
            if self.peek(1)[1] == ":=":
                name = self.next()[1]
                self.next()  # :=
                e = self.expr()
                self._swallow_decor()
                self._end_stmt()
                self.defined.add(name.upper())
                return Assign(name, e)
            if self.peek(1)[1] == ":":
                name = self.next()[1]
                self.next()  # :
                e = self.expr()
                self._swallow_decor()
                self._end_stmt()
                self.defined.add(name.upper())
                return Output(name, e)
        e = self.expr()
        self._swallow_decor()
        self._end_stmt()
        return Bare(e)

    def _swallow_decor(self):
        """吞噬语句级 `, COLORRED, LINETHICK2` 等画图修饰 (到 ; 或 EOF)。

        函数括号内的 `,` 不会被 expr 返回到这里 (已在 _call 消费),
        语句层裸 `,` 必为输出修饰。RGB(...) 带括号一并吞掉。
        """
        while self.peek()[1] == ",":
            self.next()  # ,
            # 吞掉跟随的: name [ ( ... ) ] 序列
            k, v = self.peek()
            if k == "name":
                self.next()
                if self.peek()[1] == "(":
                    depth = 0
                    while self.peek()[0] is not None:
                        _, w = self.next()
                        if w == "(":
                            depth += 1
                        elif w == ")":
                            depth -= 1
                            if depth == 0:
                                break
            elif k == "num":
                self.next()

    def _skip_draw(self):
        """跳过画图语句: 吞到匹配的顶层 ; (括号计数) 或 EOF。"""
        depth = 0
        while self.peek()[0] is not None:
            _, v = self.next()
            if v == "(":
                depth += 1
            elif v == ")":
                depth -= 1
            elif v == ";" and depth <= 0:
                return
        return

    def _end_stmt(self):
        k, v = self.peek()
        if v == ";":
            self.next()

    # ---- 表达式层 (优先级: OR < AND < 比较 < +- < */% < 一元 < 幂 < 原子)
    def expr(self):
        return self._or()

    def _or(self):
        l = self._and()
        while True:
            _, v = self.peek()
            if v == "OR" or (v and v.upper() == "OR"):
                self.next()
                l = Logic("OR", l, self._and())
            elif v in ("||", "|"):
                self.next()
                l = Logic("OR", l, self._and())
            else:
                return l

    def _and(self):
        l = self._cmp()
        while True:
            _, v = self.peek()
            if v == "AND" or (v and v.upper() == "AND"):
                self.next()
                l = Logic("AND", l, self._cmp())
            elif v in ("&&", "&"):
                self.next()
                l = Logic("AND", l, self._cmp())
            else:
                return l

    _CMPS = {">", "<", ">=", "<=", "=", "==", "<>", "!="}

    def _cmp(self):
        """链式比较: A<B<C 解析为 (A<B) AND (B<C) — TDX 数学链式语义。"""
        l = self._add()
        if self.peek()[1] not in self._CMPS:
            return l
        parts, ops = [l], []
        while self.peek()[1] in self._CMPS:
            ops.append(self.next()[1])
            parts.append(self._add())
        expr = Cmp(ops[0], parts[0], parts[1])
        for i in range(1, len(ops)):
            expr = Logic("AND", expr, Cmp(ops[i], parts[i], parts[i + 1]))
        return expr

    def _add(self):
        l = self._mul()
        while True:
            _, v = self.peek()
            if v in ("+", "-"):
                self.next()
                l = Bin(v, l, self._mul())
            else:
                return l

    def _mul(self):
        l = self._unary()
        while True:
            _, v = self.peek()
            if v in ("*", "/"):
                self.next()
                l = Bin(v, l, self._unary())
            else:
                return l

    def _unary(self):
        _, v = self.peek()
        if v == "-":
            self.next()
            return Neg(self._unary())
        if v == "!":
            self.next()
            return Not(self._unary())
        return self._pow()

    def _pow(self):
        l = self._atom()
        _, v = self.peek()
        if v == "^":
            self.next()
            return Bin("^", l, self._unary())
        return l

    def _atom(self):
        kind, v = self.next()
        if kind == "num":
            return Num(v)
        if kind == "str":
            # "MACD.DEA"(9,3,3) 形式的跨指标引用 (字符串+参数调用)
            if self._is_indref(v) and self.peek()[1] == "(":
                ind, line = self._split_indref(v)
                params = self._call_args()
                return IndRef(ind, line, params)
            return _Str(v[1:-1])
        if kind == "name":
            up = v.upper()
            if self.peek()[1] == "(":
                return self._call(v)
            # MACD.DEA 形式的跨指标引用 (裸标识符带点)
            if self.peek()[1] == "." and self.peek(1)[0] == "name":
                self.next()  # .
                line_tok = self.next()
                params = self._call_args() if self.peek()[1] == "(" else []
                return IndRef(up, line_tok[1].upper(), params)
            self._note_var(up)
            return Var(v)
        if v == "(":
            e = self.expr()
            self.expect(")")
            return e
        if v == "-":
            return Neg(self._atom())
        raise SyntaxError(f"意外的 token {v!r}")

    @staticmethod
    def _is_indref(s: str) -> bool:
        body = s[1:-1] if len(s) >= 2 and s[0] in "'\"" else s
        return "." in body and not body.replace(".", "").isdigit()

    @staticmethod
    def _split_indref(s: str):
        body = s[1:-1]
        ind, _, line = body.partition(".")
        return ind.upper(), line.upper()

    def _call_args(self):
        """吞 ( expr, ... ) 返回实参列表。"""
        self.expect("(")
        args = []
        if self.peek()[1] != ")":
            while True:
                args.append(self._arg())
                if self.peek()[1] == ",":
                    self.next()
                    continue
                break
        self.expect(")")
        return args

    def _call(self, name):
        self.expect("(")
        args = []
        if self.peek()[1] != ")":
            while True:
                args.append(self._arg())
                if self.peek()[1] == ",":
                    self.next()
                    continue
                break
        self.expect(")")
        return Call(name.upper(), args)

    def _arg(self):
        """函数实参: 表达式; 字符串为跨指标引用 ("MACD.DIF") 或纯文本。"""
        kind, v = self.peek()
        if kind == "str":
            self.next()
            if self._is_indref(v):
                ind, line = self._split_indref(v)
                params = self._call_args() if self.peek()[1] == "(" else []
                return IndRef(ind, line, params)
            return _Str(v[1:-1])
        return self.expr()

    def _note_var(self, up):
        if up not in self.defined:
            self.undefined.append(up)


class _Str(Node):
    def __init__(self, s):
        self.s = s

    def __str__(self):
        return repr(self.s)


class IndRef(Node):
    """跨指标引用: MACD.DEA / "KDJ.K"(9,3,3)。

    params 为调用实参 (数字表达式列表), 覆盖指标默认参数。
    """

    def __init__(self, ind, line, params=None):
        self.ind, self.line, self.params = ind, line, params or []

    def __str__(self):
        return f"{self.ind}.{self.line}" + (f"({self.params})" if self.params else "")


# 内置行情变量 (大小写不敏感; 不是参数, 不进 undefined)
BUILTIN_VARS = {
    "CLOSE": "close", "C": "close",
    "OPEN": "open", "O": "open",
    "HIGH": "high", "H": "high",
    "LOW": "low", "L": "low",
    "VOL": "volume", "V": "volume",
    "AMOUNT": "amount", "AMO": "amount",
}


def collect_undefined(stmts) -> set:
    """解析后收集全部未定义标识符 (语句收集 + 表达式遍历)。"""
    defined = set()
    for s in stmts:
        if isinstance(s, (Assign, Output)):
            defined.add(s.name.upper())
    undef = set()

    def walk(e):
        if isinstance(e, Var):
            if e.name.upper() not in defined \
                    and e.name.upper() not in BUILTIN_VARS:
                undef.add(e.name.upper())
        for attr in ("l", "r", "e", "expr"):
            c = getattr(e, attr, None)
            if isinstance(c, Node):
                walk(c)
        for a in getattr(e, "args", []) or []:
            if isinstance(a, Node):
                walk(a)
        for a in getattr(e, "params", []) or []:
            if isinstance(a, Node):
                walk(a)

    for s in stmts:
        e = getattr(s, "expr", None)
        if isinstance(e, Node):
            walk(e)
    return undef


def parse_formula(source: str):
    """源码 → (stmts, undefined_names)。先做语句边界修复再解析。"""
    src = preprocess(source)
    toks = tokenize(src)
    p = Parser(toks)
    stmts = p.parse()
    undef = collect_undefined(stmts)   # 全量视角 (两遍, 正确处理先引用后定义)
    # 出现序仅作参考; 只保留真正未定义的 (单遍收集可能含后续才赋值的假阳性)
    seen = [u for u in p.undefined if u in undef]
    seen += [u for u in undef if u not in seen]
    return stmts, seen


def signal_expr(stmts):
    """取信号表达式: 最后一条 Output/Bare 语句 (画图/赋值不算)。"""
    cand = [s for s in stmts if isinstance(s, (Output, Bare))]
    if not cand:
        return None
    return cand[-1]
