"""
bot/util/calculator/engine.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Safe scientific-calculator engine modelled on the Casio fx-991MS.

Backend
───────
``simpleeval`` (AST-based safe evaluation — no ``eval``, no arbitrary
code execution).  Only the explicitly whitelisted functions and names
can be referenced inside an expression.

Angle modes
───────────
Pass ``mode="deg"`` (default) or ``mode="rad"`` to ``evaluate()``.
Every trig function and its inverse respects the active mode:

    evaluate("sin(90)")              →  1.0    (DEG, default)
    evaluate("sin(1.5708)", mode="rad") →  ≈1.0  (RAD)
    evaluate("asin(1)")              →  90.0   (DEG)
    evaluate("asin(1)", mode="rad")  →  ≈1.571 (RAD)

Supported syntax
────────────────
  Arithmetic        +  -  *  /  //  %  (  )
  Exponentiation    **  or  ^   (both normalised to **)
  Trig              sin  cos  tan
  Inverse trig      sin⁻¹ / asin    cos⁻¹ / acos    tan⁻¹ / atan
  Logarithms        log (base-10)    log2 (base-2)    ln (natural)
  Exponential       exp(x) = eˣ
  Roots             √ / sqrt(x)      ∛ / cbrt(x)
  Rounding          abs   floor   ceil   round
  Combinatorics     nCr(n, r)   nPr(n, r)   n! (postfix factorial)
  Integer math      gcd(a, b)   lcm(a, b)
  Constants         pi  π   e   tau  τ   ans (last result)

Postfix factorial
─────────────────
  "5!"       →  factorial(5)       →  120
  "(2+3)!"   →  factorial((2+3))   →  120
  "5! + 3!"  →  factorial(5) + factorial(3)   →  126

Implicit multiplication
───────────────────────
  "2(3+1)"  →  "2*(3+1)"
  ")("       →  ")*("
  "2pi"      →  "2*pi"
  "2e"       →  "2*e"   (not confused with scientific notation)

Public API
──────────
  evaluate(expression, *, ans, mode) → float
      Evaluate an expression string.  Raises CalcError on any problem.

  format_result(value) → str
      Human-readable display string (comma-separated integers, ≤10 sig-figs).

  expr_str(value) → str
      Clean numeric token for re-insertion into an expression (no commas).
      Used by the = button to replace the display with the bare result.
"""

from __future__ import annotations

import math
import re

from simpleeval import EvalWithCompoundTypes, FunctionNotDefined, NameNotDefined


# ══════════════════════════════════════════════════════════════════════════════
# User-facing error
# ══════════════════════════════════════════════════════════════════════════════

class CalcError(Exception):
    """
    Raised for every user-facing problem during parsing or evaluation.

    Always carries a plain-English ``message`` safe to display verbatim,
    and an optional ``hint`` with a suggested fix shown as a second line.

    Attributes
    ----------
    message : str
        Short description of what went wrong.
    hint : str | None
        Optional one-liner suggesting how to fix it.
    """

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message: str = message
        self.hint:    str | None = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n💡 {self.hint}"
        return self.message


# ══════════════════════════════════════════════════════════════════════════════
# Domain-safe function wrappers
# ══════════════════════════════════════════════════════════════════════════════

def _factorial(x: float) -> int:
    """n! — requires a non-negative integer ≤ 170."""
    if isinstance(x, float) and not x.is_integer():
        raise CalcError(
            f"factorial requires a whole number, got {x}",
            hint="Only non-negative integers work, e.g. 5! or 10!",
        )
    n = int(x)
    if n < 0:
        raise CalcError(
            f"factorial is not defined for negative numbers (got {n})",
            hint="Try a non-negative integer instead.",
        )
    if n > 170:
        raise CalcError(
            f"{n}! is too large to compute (maximum is 170!)",
            hint="Results above 170! overflow floating point.",
        )
    return math.factorial(n)


def _ncr(n: float, r: float) -> int:
    """nCr(n, r) — number of r-combinations from n distinct items."""
    ni, ri = int(n), int(r)
    if ni != n or ri != r:
        raise CalcError(
            "nCr requires whole numbers for both arguments",
            hint=f"Try nCr({ni}, {ri}) instead.",
        )
    if ni < 0 or ri < 0:
        raise CalcError(
            f"nCr: both n and r must be ≥ 0 (got {ni}, {ri})",
        )
    if ri > ni:
        raise CalcError(
            f"nCr: r cannot be larger than n (got nCr({ni}, {ri}))",
            hint="You can't choose more items than exist in the set.",
        )
    return math.comb(ni, ri)


def _npr(n: float, r: float) -> int:
    """nPr(n, r) — number of ordered r-permutations from n distinct items."""
    ni, ri = int(n), int(r)
    if ni != n or ri != r:
        raise CalcError(
            "nPr requires whole numbers for both arguments",
            hint=f"Try nPr({ni}, {ri}) instead.",
        )
    if ni < 0 or ri < 0:
        raise CalcError(
            f"nPr: both n and r must be ≥ 0 (got {ni}, {ri})",
        )
    if ri > ni:
        raise CalcError(
            f"nPr: r cannot be larger than n (got nPr({ni}, {ri}))",
            hint="You can't arrange more items than exist in the set.",
        )
    return math.perm(ni, ri)


def _cbrt(x: float) -> float:
    """Real cube root — handles negative inputs unlike ``x ** (1/3)``."""
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def _log10(x: float) -> float:
    if x == 0:
        raise CalcError(
            "log(0) is undefined — logarithm of zero does not exist",
            hint="log approaches −∞ as x → 0. Try a positive value.",
        )
    if x < 0:
        raise CalcError(
            f"log({x}) is undefined — logarithm of a negative number is not real",
            hint="log is only defined for positive numbers.",
        )
    return math.log10(x)


def _log2(x: float) -> float:
    if x == 0:
        raise CalcError(
            "log₂(0) is undefined — logarithm of zero does not exist",
            hint="Try a positive value instead.",
        )
    if x < 0:
        raise CalcError(
            f"log₂({x}) is undefined — logarithm of a negative number is not real",
            hint="log₂ is only defined for positive numbers.",
        )
    return math.log2(x)


def _ln(x: float) -> float:
    if x == 0:
        raise CalcError(
            "ln(0) is undefined — natural log of zero does not exist",
            hint="ln approaches −∞ as x → 0. Try a positive value.",
        )
    if x < 0:
        raise CalcError(
            f"ln({x}) is undefined — natural log of a negative number is not real",
            hint="ln is only defined for positive numbers.",
        )
    return math.log(x)


def _sqrt(x: float) -> float:
    if x < 0:
        raise CalcError(
            f"√({x}) is not a real number",
            hint="Square roots of negative numbers are complex. Try abs() first, or check your expression.",
        )
    return math.sqrt(x)


def _gcd(a: float, b: float) -> int:
    """gcd(a, b) — greatest common divisor (truncates to int)."""
    ai, bi = int(a), int(b)
    if ai != a or bi != b:
        raise CalcError(
            "gcd requires whole numbers",
            hint=f"Try gcd({ai}, {bi}) instead.",
        )
    return math.gcd(ai, bi)


def _lcm(a: float, b: float) -> int:
    """lcm(a, b) — least common multiple (truncates to int)."""
    ai, bi = int(a), int(b)
    if ai != a or bi != b:
        raise CalcError(
            "lcm requires whole numbers",
            hint=f"Try lcm({ai}, {bi}) instead.",
        )
    return math.lcm(ai, bi)


# ══════════════════════════════════════════════════════════════════════════════
# Angle-mode trig factory
# ══════════════════════════════════════════════════════════════════════════════

def _build_trig(mode: str) -> dict:
    """
    Return a dict of trig functions calibrated to *mode*.

    Parameters
    ----------
    mode:
        ``"deg"`` — inputs/outputs in degrees (Casio default).
        ``"rad"`` — inputs/outputs in radians.

    Returns
    -------
    dict
        Keys ``sin``, ``cos``, ``tan``, ``asin``, ``acos``, ``atan``.
    """
    unit = "degrees" if mode == "deg" else "radians"

    if mode == "deg":
        to_r = math.radians   # user unit → radians
        to_u = math.degrees   # radians  → user unit
    else:
        def to_r(x: float) -> float: return x  # noqa: E301
        def to_u(x: float) -> float: return x  # noqa: E301

    def _sin(x: float) -> float:
        return math.sin(to_r(x))

    def _cos(x: float) -> float:
        return math.cos(to_r(x))

    def _tan(x: float) -> float:
        r = to_r(x)
        if abs(math.cos(r)) < 1e-14:
            angle_display = f"{x}°" if mode == "deg" else f"{x} rad"
            raise CalcError(
                f"tan({angle_display}) is undefined — vertical asymptote",
                hint=f"tan is undefined at 90°, 270°, … (π/2, 3π/2, … in RAD mode).",
            )
        return math.tan(r)

    def _asin(x: float) -> float:
        if not (-1.0 <= x <= 1.0):
            raise CalcError(
                f"sin⁻¹({x}) is undefined — input must be between −1 and 1",
                hint=f"sin⁻¹ is the inverse of sin, whose range is [−1, 1].",
            )
        return to_u(math.asin(x))

    def _acos(x: float) -> float:
        if not (-1.0 <= x <= 1.0):
            raise CalcError(
                f"cos⁻¹({x}) is undefined — input must be between −1 and 1",
                hint=f"cos⁻¹ is the inverse of cos, whose range is [−1, 1].",
            )
        return to_u(math.acos(x))

    def _atan(x: float) -> float:
        return to_u(math.atan(x))

    return {
        "sin":  _sin,  "cos":  _cos,  "tan":  _tan,
        "asin": _asin, "acos": _acos, "atan": _atan,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Static function / name tables (angle-agnostic)
# ══════════════════════════════════════════════════════════════════════════════

_BASE_FUNCTIONS: dict = {
    # Logarithms & exponential
    "log":  _log10,
    "log2": _log2,
    "ln":   _ln,
    "exp":  math.exp,
    # Roots
    "sqrt": _sqrt,
    "cbrt": _cbrt,
    # Rounding & magnitude
    "abs":   abs,
    "floor": math.floor,
    "ceil":  math.ceil,
    "round": round,
    # Combinatorics
    "factorial": _factorial,
    "nCr": _ncr,
    "nPr": _npr,
    # Integer math
    "gcd": _gcd,
    "lcm": _lcm,
}

_NAMES: dict = {
    "pi":  math.pi,  "π": math.pi,
    "e":   math.e,
    "tau": math.tau, "τ": math.tau,
    "ans": 0.0,       # replaced per call by evaluate()
}

# ── Typo / alias suggestions shown when an unknown name is used ───────────────
_NAME_SUGGESTIONS: dict[str, str] = {
    # Common misspellings or alternate spellings
    "sine":    "sin",    "cosine":  "cos",    "tangent": "tan",
    "arcsin":  "sin⁻¹",  "arccos":  "cos⁻¹",  "arctan":  "tan⁻¹",
    "asin":    "sin⁻¹",  "acos":    "cos⁻¹",  "atan":    "tan⁻¹",
    "sqr":     "sqrt",   "squareroot": "sqrt", "root":    "sqrt",
    "cuberoot":"cbrt",
    "natural": "ln",     "log10":   "log",
    "fac":     "!",      "fact":    "!",
    "combi":   "nCr",    "perm":    "nPr",
    "pi":      "π",      "euler":   "e",
    "absolute":"abs",    "magnitude":"abs",
    "minimum": "min",    "maximum": "max",
    "power":   "**",     "pow":     "**",
}


# ══════════════════════════════════════════════════════════════════════════════
# Expression pre-processor
# ══════════════════════════════════════════════════════════════════════════════

def _preprocess(expr: str) -> str:
    """
    Rewrite Casio-style shorthand into valid Python arithmetic.

    Applied in order (order matters):

    1. Postfix ``!``     →  ``factorial(…)``
    2. ``√`` / ``∛``      →  ``sqrt(…)`` / ``cbrt(…)``
    3. ``sin⁻¹`` etc.    →  ``asin`` etc.
    4. ``×`` / ``÷`` / ``^`` → ``*`` / ``/`` / ``**``
    5. Implicit multiplication  ``2(`` → ``2*(``  etc.
    """
    # Step 1 — factorial expansion (must run before everything else)
    expr = _expand_factorial(expr)

    # Step 2 — Unicode root symbols
    expr = re.sub(r"√\s*\(",        "sqrt(",   expr)
    expr = re.sub(r"√\s*([\w.]+)", r"sqrt(\1)", expr)
    expr = re.sub(r"∛\s*\(",        "cbrt(",   expr)
    expr = re.sub(r"∛\s*([\w.]+)", r"cbrt(\1)", expr)

    # Step 3 — Unicode superscript inverse-trig names
    expr = (
        expr
        .replace("sin⁻¹", "asin")
        .replace("cos⁻¹", "acos")
        .replace("tan⁻¹", "atan")
    )

    # Step 4 — operator aliases
    expr = expr.replace("×", "*").replace("÷", "/").replace("^", "**")

    # Step 5 — implicit multiplication
    # number or ")" followed by "(", but NOT when the digit is inside a
    # function name such as log2( — use a negative lookbehind for letters.
    expr = re.sub(r"(?<![a-zA-Z_])(\d+\.?\d*|\))(\()", r"\1*\2", expr)
    # standalone digit followed by a named constant: 2pi → 2*pi
    # negative lookbehind prevents matching log2pi → log2*pi
    expr = re.sub(r"(?<![a-zA-Z_])(\d)(pi|tau|π|τ)(?!\w)", r"\1*\2", expr)

    return expr


def _expand_factorial(expr: str) -> str:
    """
    Rewrite postfix ``!`` notation to ``factorial(…)`` calls.

    Supports:
      * ``5!``       →  ``factorial(5)``
      * ``(2+3)!``   →  ``factorial((2+3))``
      * ``5! + 3!``  →  ``factorial(5) + factorial(3)``

    Nested factorials (``(5!)!``) are not supported — write
    ``factorial(factorial(5))`` directly.

    Raises
    ------
    CalcError
        On a bare ``!`` with no valid operand, or unmatched parentheses.
    """
    out: list[str] = []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "!":
            if not out:
                raise CalcError(
                    "Unexpected '!' at the start of the expression",
                    hint="Put a number before '!', e.g. 5! or (2+3)!",
                )
            if out[-1] == ")":
                # scan backwards for the matching "("
                depth, j = 0, len(out) - 1
                while j >= 0:
                    if out[j] == ")":
                        depth += 1
                    elif out[j] == "(":
                        depth -= 1
                        if depth == 0:
                            break
                    j -= 1
                if depth != 0:
                    raise CalcError(
                        "Unmatched '(' before '!'",
                        hint="Make sure every opening bracket has a matching closing bracket.",
                    )
                operand = "".join(out[j:])
                del out[j:]
            else:
                # scan backwards over digits / decimal point
                j = len(out) - 1
                while j >= 0 and (out[j].isdigit() or out[j] == "."):
                    j -= 1
                operand = "".join(out[j + 1:])
                if not operand:
                    raise CalcError(
                        "Unexpected '!' — no number before it",
                        hint="Put a number before '!', e.g. 5! or (2+3)!",
                    )
                del out[j + 1:]
            out.append(f"factorial({operand})")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _count_parens(expr: str) -> tuple[int, int]:
    """Return (open_count, close_count) of unmatched parentheses."""
    depth = 0
    unmatched_close = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth > 0:
                depth -= 1
            else:
                unmatched_close += 1
    return depth, unmatched_close   # depth = unmatched open


def _friendly_syntax_error(raw_expr: str, proc_expr: str, exc: Exception) -> CalcError:
    """
    Convert a low-level SyntaxError / TypeError into a helpful message.

    Attempts to diagnose the most common mistakes and suggest a fix.
    Falls back to a generic but still clean message when unsure.
    """
    raw  = raw_expr.strip()
    proc = proc_expr.strip()
    msg  = str(exc)

    # ── Unmatched parentheses ─────────────────────────────────────────────
    open_un, close_un = _count_parens(proc)
    if open_un > 0:
        s = "s" if open_un > 1 else ""
        return CalcError(
            f"Missing closing bracket{'s' if open_un > 1 else ''} — {open_un} '(' opened but never closed",
            hint=f"Add {open_un} more ')' to the end of your expression.",
        )
    if close_un > 0:
        return CalcError(
            f"Unexpected ')' — more closing brackets than opening ones",
            hint="Remove the extra ')' or add a matching '(' earlier.",
        )

    # ── Trailing operator ─────────────────────────────────────────────────
    if re.search(r"[+\-*/^%]\s*$", raw):
        op = re.search(r"([+\-*/^%])\s*$", raw).group(1)
        return CalcError(
            f"Expression ends with '{op}' — nothing to the right of the operator",
            hint=f"Add a number after '{op}', or remove it.",
        )

    # ── Leading operator (other than unary minus) ─────────────────────────
    if re.match(r"^\s*[+*/^%]", raw):
        op = re.match(r"^\s*([+*/^%])", raw).group(1)
        return CalcError(
            f"Expression starts with '{op}' — nothing to the left of the operator",
            hint="Start with a number or opening bracket instead.",
        )

    # ── Double operator ───────────────────────────────────────────────────
    m = re.search(r"([+\-*/^%])\s*([+*/^%])", raw)
    if m:
        return CalcError(
            f"Two operators in a row: '{m.group(1)}{m.group(2)}'",
            hint=f"Did you mean to write one operator, or put a number between them?",
        )

    # ── Empty brackets ────────────────────────────────────────────────────
    if "()" in proc:
        return CalcError(
            "Empty brackets '()' — nothing inside",
            hint="Put an expression inside the brackets, e.g. (2 + 3).",
        )

    # ── Missing operator between tokens ──────────────────────────────────
    # e.g. "2 3" or ") 5"
    if re.search(r"\d\s+\d", raw) or re.search(r"\)\s+\d", raw):
        return CalcError(
            "Missing operator between terms",
            hint="Add '+', '-', '*', or '/' between the numbers, e.g. 2 * 3.",
        )

    # ── Generic fallback (still clean) ───────────────────────────────────
    return CalcError(
        "Invalid expression — couldn't parse it",
        hint="Check for typos, mismatched brackets, or missing operators.",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def evaluate(expression: str, *, ans: float = 0.0, mode: str = "deg") -> float:
    """
    Evaluate a Casio-style scientific expression and return the result.

    Parameters
    ----------
    expression:
        Raw expression string.  Casio shorthand (``^``, ``×``, ``√``,
        ``sin⁻¹``, postfix ``!``, …) is normalised transparently.
    ans:
        Value substituted for the ``ans`` token — should be the result of
        the previous successful call.  Defaults to ``0.0``.
    mode:
        Trig angle unit: ``"deg"`` (default) or ``"rad"``.

    Returns
    -------
    float
        The numeric result.

    Raises
    ------
    CalcError
        On any user-facing problem: empty input, syntax errors, domain
        violations, unknown names, division by zero, non-finite results.

    Examples
    --------
    >>> evaluate("sin(45) + sqrt(16)")          # DEG mode
    6.414213562373095
    >>> evaluate("nCr(10, 3)")
    120.0
    >>> evaluate("(2+3)!")
    120.0
    >>> evaluate("2pi")                          # implicit multiplication
    6.283185307179586
    >>> evaluate("ans * 2", ans=21.0)
    42.0
    """
    if not expression or not expression.strip():
        raise CalcError(
            "Nothing to calculate — expression is empty",
            hint="Type a number or expression, e.g. sin(45) or 2^10.",
        )

    raw_expr = expression
    try:
        expr = _preprocess(expression)
    except CalcError:
        raise  # re-raise clean errors from _expand_factorial

    names     = {**_NAMES, "ans": ans}
    functions = {**_BASE_FUNCTIONS, **_build_trig(mode)}
    evaluator = EvalWithCompoundTypes(functions=functions, names=names)

    try:
        result = evaluator.eval(expr)
    except ZeroDivisionError:
        raise CalcError(
            "Division by zero",
            hint="The denominator evaluated to zero. Check your expression.",
        ) from None
    except NameNotDefined as exc:
        name  = exc.name
        lower = name.lower()
        suggestion = _NAME_SUGGESTIONS.get(lower)
        if suggestion:
            raise CalcError(
                f"'{name}' is not recognised",
                hint=f"Did you mean '{suggestion}'?",
            ) from None
        raise CalcError(
            f"Unknown name '{name}'",
            hint="Check the spelling, or use the ƒ(x) menu to insert functions.",
        ) from None
    except FunctionNotDefined as exc:
        name  = exc.func_name
        lower = name.lower()
        suggestion = _NAME_SUGGESTIONS.get(lower)
        if suggestion:
            raise CalcError(
                f"'{name}(…)' is not a recognised function",
                hint=f"Did you mean '{suggestion}'?",
            ) from None
        raise CalcError(
            f"Unknown function '{name}'",
            hint="Check the spelling, or use the ƒ(x) menu to insert functions.",
        ) from None
    except CalcError:
        raise
    except (SyntaxError, TypeError) as exc:
        raise _friendly_syntax_error(raw_expr, expr, exc) from None
    except ValueError as exc:
        raise CalcError(
            f"Invalid value — {exc}",
            hint="Check that your inputs are in the right range for this function.",
        ) from None
    except OverflowError:
        raise CalcError(
            "Result is too large to compute (overflow)",
            hint="The number exceeded the maximum representable float (~1.8 × 10³⁰⁸).",
        ) from None

    if not isinstance(result, (int, float)):
        raise CalcError(
            "Expression did not produce a number",
            hint="Make sure the expression evaluates to a single numeric value.",
        )
    if isinstance(result, float) and math.isnan(result):
        raise CalcError(
            "Result is Not a Number (NaN)",
            hint="This usually means an indeterminate form like 0/0 or ∞−∞.",
        )
    if isinstance(result, float) and math.isinf(result):
        sign = "positive" if result > 0 else "negative"
        raise CalcError(
            f"Result is {sign} infinity — the value is too large",
            hint="The expression overflows to ±∞. Check for a very large exponent or near-zero denominator.",
        )

    return float(result)


def format_result(value: float) -> str:
    """
    Format a number for human-readable **display** (e.g. in an embed field).

    Rules
    -----
    * Exact integers below 10¹⁵ are shown without a decimal point and
      with thousands-separator commas: ``1234567`` → ``"1,234,567"``.
    * Floats that equal an integer use the same integer format.
    * All other floats use up to 10 significant figures, trailing zeros
      stripped (Python ``:.10g`` format).

    Examples
    --------
    >>> format_result(120)
    '120'
    >>> format_result(1234567.0)
    '1,234,567'
    >>> format_result(3.141592653589793)
    '3.141592654'
    """
    if isinstance(value, int):
        return f"{value:,}"
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:.10g}"


def expr_str(value: float) -> str:
    """
    Format a number as a clean **expression token** (no commas, compact).

    Unlike ``format_result``, this produces a string that is safe to
    re-insert into a new expression string.  Comma-separated strings like
    ``"1,234"`` would cause a syntax error if typed back in.

    Used by the ``=`` button to replace the expression field with the
    bare numeric result so further key-presses produce a valid expression.

    Examples
    --------
    >>> expr_str(120)
    '120'
    >>> expr_str(1234567.0)
    '1234567'
    >>> expr_str(3.141592653589793)
    '3.141592654'
    """
    if isinstance(value, int):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"