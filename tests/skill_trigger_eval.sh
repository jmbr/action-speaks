#!/usr/bin/env bash
# Evaluate whether the skill triggers on relevant prompts and ignores near misses.
# The name "action-speaks" carries no mathematical meaning, so the description must do the
# routing. Each run starts Copilot CLI in an empty directory, so this repository's own
# instructions cannot influence the result.
# Usage: tests/skill_trigger_eval.sh [runs] [main|holdout]   (default 1 main)
# PARALLEL (default 2) bounds concurrent agents; each may start a multi-gigabyte Lean session.
set -uo pipefail

RUNS="${1:-1}"
SET="${2:-main}"
MARKER="skill(action-speaks)"
PARALLEL="${PARALLEL:-2}"

# Positives avoid words like "Lean" and "prove"; those prompts would be too easy.
MAIN=(
"yes|square-bound|Is it true that for every natural number n greater than 5, n squared exceeds 25? I need to be sure."
"yes|am-gm|My colleague claims that for all positive reals a and b, (a+b)/2 is at least sqrt(ab). Is that airtight?"
"yes|quadratic|Is x^2 - 6x + 10 positive for every real x? Double-check before I put it in the paper."
"yes|odd-sum|I claim the sum of the first n odd numbers is always n^2. Can you confirm that holds for every n?"
"yes|exp-vs-cube|Is 2^n > n^3 for all n >= 10? Give me an answer I can trust."
"yes|termination|Does this always terminate for natural n? def f(n): return 0 if n == 0 else f(n // 2)"
"yes|markov|Does P(X >= a) <= E[X]/a hold for every nonnegative random variable X and every a > 0? Check it rigorously before I rely on it."
# Near misses share vocabulary with the skill but require a different task.
"no|arithmetic|What is 37 * 43?"
"no|prime-fn|Write a Python function that checks whether a number is prime."
"no|plot|Plot y = x^2 - 6x + 10 for x between 0 and 6."
"no|explain|Explain intuitively, to a high-school student, why the harmonic series diverges."
"no|lake-error|Our CI fails with 'unknown package Mathlib' after editing lakefile.toml. How do I fix the build?"
"no|latex|Typeset this in LaTeX: the sum from k=1 to n of k^2 equals n(n+1)(2n+1)/6."
# Statistics on observed data belongs to a hypothesis-testing tool, not to a prover.
"no|ab-test|Control converted 120 of 2400 visitors and the variant 151 of 2400. Is the lift real or noise?"
)

# Written after the main set; keep it unseen while tuning the description.
HOLDOUT=(
"yes|sine-bound|A reviewer says my bound |sin x| <= |x| for all real x is wrong. Who is right?"
"yes|midpoint|In my binary search on naturals, lo <= hi at loop entry. Does mid = (lo + hi) / 2 always stay within [lo, hi]?"
"yes|counterexample|Every odd number greater than 1 is prime, right? Show me if that's wrong."
"yes|divisibility|Is n^5 - n divisible by 30 for every integer n? I'm citing it in a proof sketch."
"yes|chebyshev|For any distribution with finite variance, is the chance of landing two standard deviations from the mean at most 1/4? Make sure."
"no|percent|What's 15% of 2400?"
"no|history|Summarize the history of Fermat's Last Theorem."
"no|unit-tests|Write pytest unit tests for a function that sorts a list of integers."
"no|sample-size|How many users per arm do I need to detect a lift from 10% to 12% conversion?"
)

case "$SET" in
  main) QUERIES=("${MAIN[@]}") ;;
  holdout) QUERIES=("${HOLDOUT[@]}") ;;
  *) echo "unknown set: $SET (use main or holdout)" >&2; exit 2 ;;
esac

run_one() {
  local expect="$1" label="$2" query="$3" hits=0 dir
  for _ in $(seq 1 "$RUNS"); do
    dir="$(mktemp -d)"
    if (cd "$dir" && timeout 300 copilot -p "$query" --allow-all-tools 2>&1) | grep -qF "$MARKER"; then
      hits=$((hits + 1))
    fi
    rm -rf "$dir"
  done
  local rate verdict
  rate=$(awk -v h="$hits" -v r="$RUNS" 'BEGIN{printf "%.2f", h/r}')
  if [ "$expect" = yes ]; then
    awk -v x="$rate" 'BEGIN{exit !(x > 0.5)}' && verdict=PASS || verdict=FAIL
  else
    awk -v x="$rate" 'BEGIN{exit !(x <= 0.5)}' && verdict=PASS || verdict=FAIL
  fi
  printf '%-4s expect=%-3s rate=%s  %s\n' "$verdict" "$expect" "$rate" "$label"
}

export -f run_one
export RUNS MARKER

RESULTS="$(mktemp)"
trap 'rm -f "$RESULTS"' EXIT

printf 'Running %s set: %d queries x %d run(s)\n\n' "$SET" "${#QUERIES[@]}" "$RUNS"
# NUL-separated and passed as an argument: xargs would otherwise strip quotes such as the
# apostrophe in "what's", and splicing the query into the script would let it run as code.
printf '%s\0' "${QUERIES[@]}" \
  | xargs -0 -n 1 -P "$PARALLEL" bash -c 'IFS="|" read -r e l q <<< "$1"; run_one "$e" "$l" "$q"' _ \
  | sort -k4 | tee "$RESULTS"

echo
awk '{print $1}' "$RESULTS" | sort | uniq -c
