"""Realistic scenario: an agent asked to justify claims about a numerical method."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leanai.repl import Session
from leanai.verify import Verifier
from leanai import search as S

s = Session(); s.start(); vf = Verifier(s)
print(f"session ready {s.startup_seconds:.2f}s\n")

# Claim 1: an agent claims AM-GM for two reals.
claim1 = "For nonnegative reals a,b: sqrt(a*b) <= (a+b)/2"
src1 = """theorem am_gm_two (a b : ℝ) (ha : 0 ≤ a) (hb : 0 ≤ b) :
    Real.sqrt (a * b) ≤ (a + b) / 2 := by
  rw [show a * b = ((a+b)/2)^2 - ((a-b)/2)^2 by ring]
  calc Real.sqrt (((a+b)/2)^2 - ((a-b)/2)^2) ≤ Real.sqrt (((a+b)/2)^2) := by
        apply Real.sqrt_le_sqrt; nlinarith [sq_nonneg ((a-b)/2)]
    _ = |(a+b)/2| := Real.sqrt_sq_eq_abs _
    _ = (a+b)/2 := abs_of_nonneg (by linarith)"""
v1 = vf.verify(src1, claim=claim1, require_nontrivial=True)
print(v1.render()); print()

# Claim 2: THE SUBTLE TRAP. Agent claims "n - 1 < n for all naturals".
# True over ℤ/ℝ, but ℕ subtraction truncates: 0 - 1 = 0, so it is FALSE at n = 0.
claim2 = "For every natural n, n - 1 < n"
src2 = "theorem nat_sub_lt (n : ℕ) : n - 1 < n := by omega"
v2 = vf.verify(src2, claim=claim2)
print(v2.render()); print()

# The agent must repair the statement, not the proof.
src2b = "theorem nat_sub_lt' (n : ℕ) (hn : 0 < n) : n - 1 < n := by omega"
v2b = vf.verify(src2b, claim="For every POSITIVE natural n, n - 1 < n", require_nontrivial=True)
print(v2b.render()); print()

# Claim 3: agent overreaches - claims a bound that needs a hypothesis it forgot to use.
v3 = vf.verify("theorem sq_nonneg' (x : ℝ) (hx : 0 < x) : 0 ≤ x^2 := sq_nonneg x",
               claim="For positive x, x squared is nonnegative", require_nontrivial=True)
print(v3.render())
s.close()
