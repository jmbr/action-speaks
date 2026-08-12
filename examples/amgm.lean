theorem am_gm_two (a b : ℝ) (ha : 0 ≤ a) (hb : 0 ≤ b) :
    Real.sqrt (a * b) ≤ (a + b) / 2 := by
  rw [show a * b = ((a+b)/2)^2 - ((a-b)/2)^2 by ring]
  calc Real.sqrt (((a+b)/2)^2 - ((a-b)/2)^2) ≤ Real.sqrt (((a+b)/2)^2) := by
        apply Real.sqrt_le_sqrt; nlinarith [sq_nonneg ((a-b)/2)]
    _ = |(a+b)/2| := Real.sqrt_sq_eq_abs _
    _ = (a+b)/2 := abs_of_nonneg (by linarith)
