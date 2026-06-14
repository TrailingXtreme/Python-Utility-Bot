"""
bot/util/calculator/engine.py
──────────────────────────────
Safe scientific-calculator engine, modelled after Casio fx-991MS functions.

Built on `simpleeval` (AST-based safe eval — no arbitrary code execution).
Replaces TagScriptEngine/advancedtagscriptengine for the Calculator cog.

Trig functions operate in DEGREES by default (Casio convention), matching
the `DEG`/`RAD` mode toggle on real calculators.
"""

import math
import re

from simpleeval import EvalWithCompoundTypes, FunctionNotDefined, NameNotDefined


class CalcError(Exception):
    """Raised for any user-facing calculator error (bad syntax, domain error)."""


def _factorial(x: float) -> int:
    n = int(x)
    if n != x or n < 0:
        raise CalcError("factorial requires a non-negative integer")
    if n > 170:
        raise CalcError("factorial result too large")
    return math.factorial(n)


def _ncr(n: float, r: float) -> int:
    n, r = int(n), int(r)
    if r < 0 or n < 0 or r > n:
        raise CalcError("invalid nCr arguments")
    return math.comb(n, r)


def _npr(n: float, r: float) -> int:
    n, r = int(n), int(r)
    if r < 0 or n < 0 or r > n:
        raise CalcError("invalid nPr arguments")
    return math.perm(n, r)


# ── DEG-mode trig wrappers (Casio default) ────────────────────────────────────
def _sin(x: float) -> float:
    return math.sin(math.radians(x))


def _cos(x: float) -> float:
    return math.cos(math.radians(x))


def _tan(x: float) -> float:
    return math.tan(math.radians(x))


def _asin(x: float) -> float:
    return math.degrees(math.asin(x))


def _acos(x: float) -> float:
    return math.degrees(math.acos(x))


def _atan(x: float) -> float:
    return math.degrees(math.atan(x))


def _log10(x: float) -> float:
    if x <= 0:
        raise CalcError("log domain error: x must be > 0")
    return math.log10(x)


def _ln(x: float) -> float:
    if x <= 0:
        raise CalcError("ln domain error: x must be > 0")
    return math.log(x)


def _sqrt(x: float) -> float:
    if x < 0:
        raise CalcError("sqrt domain error: x must be >= 0")
    return math.sqrt(x)


_FUNCTIONS = {
    "sin": _sin, "cos": _cos, "tan": _tan,
    "asin": _asin, "acos": _acos, "atan": _atan,
    "log": _log10, "ln": _ln,
    "sqrt": _sqrt,
    "abs": abs, "round": round,
    "factorial": _factorial,
    "nCr": _ncr, "nPr": _npr,
    "exp": math.exp,
}

_NAMES = {
    "pi": math.pi, "π": math.pi,
    "e": math.e,
    "tau": math.tau,
    "ans": 0.0,  # overridden per-evaluation with last result
}


def evaluate(expression: str, *, ans: float = 0.0) -> float:
    """
    Evaluate a Casio-style scientific expression.

    Supports: + - * / ** % () sin cos tan asin acos atan log ln sqrt √
    factorial(x) or x! , nCr nPr, abs, round, pi/π, e, tau, ans (last result).

    Trig functions are in DEGREES. Raises CalcError on any problem.
    """
    if not expression or not expression.strip():
        raise CalcError("empty expression")

    # Casio-style postfix factorial: "5!" -> "factorial(5)"
    expr = _expand_factorial(expression)

    # √ is not a valid Python identifier — rewrite "√16" / "√(16)" -> "sqrt(16)"
    expr = re.sub(r"√\s*\(", "sqrt(", expr)
    expr = re.sub(r"√\s*([\w.]+)", r"sqrt(\1)", expr)

    # sin⁻¹/cos⁻¹/tan⁻¹ are not valid identifiers — rewrite to asin/acos/atan
    expr = expr.replace("sin⁻¹", "asin").replace("cos⁻¹", "acos").replace("tan⁻¹", "atan")

    # Casio-style implicit multiplication / operator aliases
    expr = expr.replace("×", "*").replace("÷", "/").replace("^", "**")

    names = dict(_NAMES)
    names["ans"] = ans

    evaluator = EvalWithCompoundTypes(functions=_FUNCTIONS, names=names)

    try:
        result = evaluator.eval(expr)
    except ZeroDivisionError:
        raise CalcError("division by zero") from None
    except NameNotDefined as exc:
        raise CalcError(f"unknown name: {exc.name}") from None
    except FunctionNotDefined as exc:
        raise CalcError(f"unknown function: {exc.func_name}") from None
    except CalcError:
        raise
    except (SyntaxError, TypeError, ValueError, OverflowError) as exc:
        raise CalcError(f"invalid expression ({exc.__class__.__name__})") from None

    if not isinstance(result, (int, float)):
        raise CalcError("expression did not evaluate to a number")
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        raise CalcError("result is not a finite number")

    return result


def _expand_factorial(expr: str) -> str:
    """
    Rewrite postfix '!' notation to factorial() calls.

    Handles simple numeric/paren operands: '5!' -> 'factorial(5)',
    '(2+3)!' -> 'factorial((2+3))'. Nested/chained factorials are not
    supported — use factorial(x) directly for those.
    """
    out: list[str] = []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "!":
            # find the operand immediately before '!'
            if not out:
                raise CalcError("unexpected '!'")
            if out[-1] == ")":
                depth = 0
                j = len(out) - 1
                while j >= 0:
                    if out[j] == ")":
                        depth += 1
                    elif out[j] == "(":
                        depth -= 1
                        if depth == 0:
                            break
                    j -= 1
                if depth != 0:
                    raise CalcError("unbalanced parentheses before '!'")
                operand = "".join(out[j:])
                del out[j:]
                out.append(f"factorial({operand})")
            else:
                j = len(out) - 1
                while j >= 0 and (out[j].isdigit() or out[j] == "."):
                    j -= 1
                operand = "".join(out[j + 1:])
                if not operand:
                    raise CalcError("unexpected '!'")
                del out[j + 1:]
                out.append(f"factorial({operand})")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def format_result(value: float) -> str:
    """Format a result the way a Casio display would: trim trailing zeros."""
    if isinstance(value, int):
        return f"{value:,}"
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:.10g}"