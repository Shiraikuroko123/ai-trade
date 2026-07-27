from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..models import Bar


"""Safe, allowlisted factor expression language.

A deliberately tiny arithmetic language over completed daily bars. There is
no attribute access, no names outside the allowlist, no strings, no calls to
anything but the fixed window functions, and hard caps on length, token
count, and nesting depth — an expression can compute a number from history
and nothing else. Series align on their last element; the final value is the
last element of the resulting series.
"""


MAX_EXPRESSION_LENGTH = 200
MAX_TOKENS = 80
MAX_DEPTH = 12
MAX_WINDOW = 500

SERIES_IDENTIFIERS = ("open", "close", "high", "low", "volume", "amount")
FUNCTIONS = {
    # name: (minimum window, extra lookback need beyond the argument's)
    "sma": (2, "n - 1"),
    "std": (2, "n - 1"),
    "ts_max": (1, "n - 1"),
    "ts_min": (1, "n - 1"),
    "delay": (1, "n"),
    "ret": (1, "n"),
    # Additive allowlist extension: time-series operators in the style of
    # the public Alpha101/Qlib expression sets. All stay single-series with
    # an integer window, so the grammar, canonical form, and every existing
    # stored expression are unchanged.
    "ts_sum": (1, "n - 1"),
    "ts_rank": (2, "n - 1"),
    "ts_argmax": (2, "n - 1"),
    "ts_argmin": (2, "n - 1"),
    "delta": (1, "n"),
}

# Functions whose extra lookback is the full window (they reach back to the
# value n sessions ago) rather than window - 1 (rolling aggregations).
_FULL_WINDOW_LOOKBACK = frozenset({"delay", "ret", "delta"})

_ALLOWED_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_+-*/(), .")


class ExpressionError(ValueError):
    pass


class _Unavailable(Exception):
    pass


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


@dataclass(frozen=True)
class CompiledExpression:
    source: str
    minimum_history: int
    _root: Any

    def compute(self, history: Sequence[Bar]) -> float | None:
        if len(history) < self.minimum_history:
            return None
        series = {
            "open": [bar.open for bar in history],
            "close": [bar.close for bar in history],
            "high": [bar.high for bar in history],
            "low": [bar.low for bar in history],
            "volume": [bar.volume for bar in history],
            "amount": [bar.amount for bar in history],
        }
        try:
            value = _evaluate(self._root, series)
        except _Unavailable:
            return None
        if isinstance(value, list):
            if not value:
                return None
            value = value[-1]
        result = float(value)
        if result != result or abs(result) == float("inf"):
            return None
        return result


def compile_expression(source: str) -> CompiledExpression:
    if not isinstance(source, str) or not source.strip():
        raise ExpressionError("表达式不能为空")
    text = source.strip().lower()
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(
            f"表达式长度不能超过 {MAX_EXPRESSION_LENGTH} 个字符"
        )
    unsupported = set(text) - _ALLOWED_CHARACTERS
    if unsupported:
        raise ExpressionError(
            "表达式包含不允许的字符: " + "".join(sorted(unsupported))[:20]
        )
    tokens = _tokenize(text)
    if len(tokens) > MAX_TOKENS:
        raise ExpressionError(f"表达式不能超过 {MAX_TOKENS} 个记号")
    parser = _Parser(tokens)
    root = parser.parse()
    minimum_history = _required_history(root)
    canonical = _canonical(tokens)
    return CompiledExpression(canonical, minimum_history, root)


def _canonical(tokens: list[_Token]) -> str:
    """Whitespace-free canonical form so equal designs hash identically."""
    parts: list[str] = []
    for token in tokens:
        if token.kind == "," and parts:
            parts.append(",")
        else:
            parts.append(token.value)
    return "".join(parts)


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == " ":
            index += 1
            continue
        if char in "+-*/(),":
            tokens.append(_Token(char, char))
            index += 1
            continue
        if char.isdigit() or char == ".":
            start = index
            while index < len(text) and (text[index].isdigit() or text[index] == "."):
                index += 1
            raw = text[start:index]
            try:
                float(raw)
            except ValueError as exc:
                raise ExpressionError(f"无法解析数字: {raw}") from exc
            tokens.append(_Token("number", raw))
            continue
        if char.isalpha() or char == "_":
            start = index
            while index < len(text) and (
                text[index].isalnum() or text[index] == "_"
            ):
                index += 1
            tokens.append(_Token("ident", text[start:index]))
            continue
        raise ExpressionError(f"无法解析的字符: {char!r}")
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.position = 0

    def parse(self):
        root = self._expression(0)
        if self.position != len(self.tokens):
            raise ExpressionError("表达式在结尾处有多余内容")
        return root

    def _peek(self) -> _Token | None:
        if self.position < len(self.tokens):
            return self.tokens[self.position]
        return None

    def _next(self) -> _Token:
        token = self._peek()
        if token is None:
            raise ExpressionError("表达式意外结束")
        self.position += 1
        return token

    def _expression(self, depth: int):
        if depth > MAX_DEPTH:
            raise ExpressionError(f"表达式嵌套不能超过 {MAX_DEPTH} 层")
        node = self._term(depth + 1)
        while True:
            token = self._peek()
            if token is None or token.kind not in {"+", "-"}:
                return node
            self._next()
            node = (token.kind, node, self._term(depth + 1))

    def _term(self, depth: int):
        if depth > MAX_DEPTH:
            raise ExpressionError(f"表达式嵌套不能超过 {MAX_DEPTH} 层")
        node = self._unary(depth + 1)
        while True:
            token = self._peek()
            if token is None or token.kind not in {"*", "/"}:
                return node
            self._next()
            node = (token.kind, node, self._unary(depth + 1))

    def _unary(self, depth: int):
        if depth > MAX_DEPTH:
            raise ExpressionError(f"表达式嵌套不能超过 {MAX_DEPTH} 层")
        token = self._peek()
        if token is not None and token.kind == "-":
            self._next()
            return ("neg", self._unary(depth + 1))
        return self._atom(depth + 1)

    def _atom(self, depth: int):
        token = self._next()
        if token.kind == "number":
            return ("number", float(token.value))
        if token.kind == "(":
            node = self._expression(depth + 1)
            closing = self._next()
            if closing.kind != ")":
                raise ExpressionError("缺少右括号")
            return node
        if token.kind != "ident":
            raise ExpressionError(f"意外的记号: {token.value!r}")
        name = token.value
        following = self._peek()
        if following is not None and following.kind == "(":
            if name not in FUNCTIONS:
                raise ExpressionError(f"未知函数: {name}")
            self._next()
            argument = self._expression(depth + 1)
            comma = self._next()
            if comma.kind != ",":
                raise ExpressionError(f"{name} 需要窗口参数，例如 {name}(close, 20)")
            window_token = self._next()
            if window_token.kind != "number" or "." in window_token.value:
                raise ExpressionError(f"{name} 的窗口必须是整数字面量")
            window = int(window_token.value)
            minimum, _need = FUNCTIONS[name]
            if not minimum <= window <= MAX_WINDOW:
                raise ExpressionError(
                    f"{name} 的窗口必须在 {minimum} 到 {MAX_WINDOW} 之间"
                )
            closing = self._next()
            if closing.kind != ")":
                raise ExpressionError("缺少右括号")
            return ("call", name, argument, window)
        if name not in SERIES_IDENTIFIERS:
            raise ExpressionError(
                "未知标识符: "
                + name
                + "；可用序列: "
                + ", ".join(SERIES_IDENTIFIERS)
            )
        return ("series", name)


def _required_history(node) -> int:
    kind = node[0]
    if kind == "number":
        return 1
    if kind == "series":
        return 1
    if kind == "neg":
        return _required_history(node[1])
    if kind == "call":
        _name, name, argument, window = "call", node[1], node[2], node[3]
        base = _required_history(argument)
        if name in _FULL_WINDOW_LOOKBACK:
            return base + window
        return base + window - 1
    _operator, left, right = node
    return max(_required_history(left), _required_history(right))


def _evaluate(node, series: dict[str, list[float]]):
    kind = node[0]
    if kind == "number":
        return node[1]
    if kind == "series":
        return list(series[node[1]])
    if kind == "neg":
        value = _evaluate(node[1], series)
        if isinstance(value, list):
            return [-item for item in value]
        return -value
    if kind == "call":
        name, argument, window = node[1], node[2], node[3]
        value = _evaluate(argument, series)
        if not isinstance(value, list):
            raise ExpressionError(f"{name} 的第一个参数必须是序列")
        if len(value) < (
            window + 1 if name in _FULL_WINDOW_LOOKBACK else window
        ):
            raise _Unavailable()
        if name == "sma":
            return _rolling(value, window, lambda chunk: sum(chunk) / window)
        if name == "std":
            def _std(chunk: list[float]) -> float:
                mean = sum(chunk) / window
                return (
                    sum((item - mean) ** 2 for item in chunk) / (window - 1)
                ) ** 0.5
            return _rolling(value, window, _std)
        if name == "ts_max":
            return _rolling(value, window, max)
        if name == "ts_min":
            return _rolling(value, window, min)
        if name == "delay":
            return value[:-window]
        if name == "ret":
            shifted = value[:-window]
            current = value[window:]
            return [
                _divide(current[index], shifted[index]) - 1.0
                for index in range(len(shifted))
            ]
        if name == "ts_sum":
            return _rolling(value, window, sum)
        if name == "ts_rank":
            def _rank(chunk: list[float]) -> float:
                last = chunk[-1]
                below = sum(1 for item in chunk if item < last)
                equal = sum(1 for item in chunk if item == last)
                midrank = below + (equal + 1) / 2
                return (midrank - 1.0) / (window - 1)
            return _rolling(value, window, _rank)
        if name == "ts_argmax":
            def _argmax(chunk: list[float]) -> float:
                peak = max(chunk)
                for offset in range(len(chunk)):
                    if chunk[-1 - offset] == peak:
                        return float(offset)
                return float(len(chunk) - 1)
            return _rolling(value, window, _argmax)
        if name == "ts_argmin":
            def _argmin(chunk: list[float]) -> float:
                trough = min(chunk)
                for offset in range(len(chunk)):
                    if chunk[-1 - offset] == trough:
                        return float(offset)
                return float(len(chunk) - 1)
            return _rolling(value, window, _argmin)
        if name == "delta":
            shifted = value[:-window]
            current = value[window:]
            return [
                current[index] - shifted[index]
                for index in range(len(shifted))
            ]
        raise ExpressionError(f"未知函数: {name}")
    operator, left_node, right_node = node
    left = _evaluate(left_node, series)
    right = _evaluate(right_node, series)
    return _combine(operator, left, right)


def _rolling(values: list[float], window: int, reducer) -> list[float]:
    return [
        reducer(values[index - window : index])
        for index in range(window, len(values) + 1)
    ]


def _combine(operator: str, left, right):
    if isinstance(left, list) and isinstance(right, list):
        length = min(len(left), len(right))
        left_tail = left[-length:]
        right_tail = right[-length:]
        return [
            _apply(operator, left_tail[index], right_tail[index])
            for index in range(length)
        ]
    if isinstance(left, list):
        return [_apply(operator, item, right) for item in left]
    if isinstance(right, list):
        return [_apply(operator, left, item) for item in right]
    return _apply(operator, left, right)


def _apply(operator: str, left: float, right: float) -> float:
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    return _divide(left, right)


def _divide(left: float, right: float) -> float:
    if right == 0:
        raise _Unavailable()
    return left / right


__all__ = [
    "CompiledExpression",
    "ExpressionError",
    "FUNCTIONS",
    "MAX_EXPRESSION_LENGTH",
    "SERIES_IDENTIFIERS",
    "compile_expression",
]
