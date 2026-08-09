"""Run the same 40-question benchmark against five NVIDIA API keys concurrently.

Keys are read only from the local .env file supplied on the command line.  They
are never written to the output file or printed.
"""

import argparse
import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = "meta/llama-3.1-8b-instruct"

QUESTIONS = [
    # Finance
    "A $10,000 investment grows 7% annually for 5 years. What is its value? Show the formula.",
    "Explain the difference between a stock's market capitalization and enterprise value in two sentences.",
    "A bond has $1,000 face value, 5% annual coupon, and trades at $950. What is its current yield?",
    "What happens to the present value of a fixed future cash flow when the discount rate rises? Why?",
    "Calculate the simple return when a stock rises from $80 to $92 and pays a $1 dividend.",
    "Name two risks that diversification reduces and one it generally cannot eliminate.",
    "If annual inflation is 3% and a savings account yields 4%, approximate the real return.",
    "Explain why a company can report positive net income but negative operating cash flow.",
    # Physics
    "A 2 kg object accelerates at 3 m/s^2. What net force acts on it?",
    "A ball is thrown straight up at 20 m/s. Ignoring air resistance, what maximum height does it reach? Use g=9.8 m/s^2.",
    "State conservation of energy and give a simple everyday example.",
    "Light has wavelength 500 nm. Calculate its frequency using c=3.0e8 m/s.",
    "What is the difference between speed and velocity?",
    "A 10-ohm resistor carries 2 A. Find its voltage and power.",
    "Explain in one paragraph why satellites remain in orbit instead of falling straight down.",
    "What change in temperature occurs when 500 J heats 0.1 kg of water with c=4186 J/(kg K)?",
    # Math
    "Solve 3x - 7 = 20.",
    "Differentiate f(x)=x^3 - 4x^2 + 6x.",
    "Integrate 2x + 3 from x=0 to x=4.",
    "A fair six-sided die is rolled twice. What is the probability the sum is 7?",
    "Find the hypotenuse of a right triangle with legs 5 and 12.",
    "Solve the quadratic x^2 - 5x + 6 = 0.",
    "What is the mean and median of 2, 4, 4, 8, 12?",
    "Simplify (a^3 b^-2)/(a b^-1).",
    # Programming
    "Write a Python function that returns True when a string is a palindrome, ignoring case and spaces.",
    "Explain the time complexity of binary search and why it has that complexity.",
    "In JavaScript, show a safe way to parse JSON that may be invalid.",
    "What is the practical difference between a process and a thread?",
    "Write SQL to return the top 3 highest-paid employees from employees(name, salary).",
    "Explain what a race condition is and give a concise example.",
    "What is the difference between HTTP PUT and PATCH?",
    "Write a Git command sequence to create a branch, commit staged changes, and push it to origin.",
    # General reasoning / research
    "Give a three-step method for judging whether an online source is trustworthy.",
    "Explain correlation versus causation with a short example.",
    "Summarize the scientific method in four concise steps.",
    "List three ways a small business can improve cash flow without taking debt.",
    "Compare qualitative and quantitative research in a compact table.",
    "What are two common cognitive biases that can distort an investment decision?",
    "Give a five-point checklist for reviewing an AI-generated answer before relying on it.",
    "Explain opportunity cost using a personal decision example.",
]


def load_env(path: Path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ask(key_number, api_key, question_number, question):
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": question}],
        "temperature": 0.2,
        "max_tokens": 600,
    }).encode()
    request = urllib.request.Request(
        ENDPOINT, data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.load(response)
            return {"key": key_number, "question": question_number, "ok": True,
                    "answer": body["choices"][0]["message"].get("content", "")}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return {"key": key_number, "question": question_number, "ok": False,
                    "status": exc.code, "error": detail}
        except Exception as exc:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return {"key": key_number, "question": question_number, "ok": False,
                    "error": str(exc)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--output", default="nvidia_parallel_results.json", type=Path)
    args = parser.parse_args()
    load_env(args.env)
    keys = [(n, os.environ.get(f"NVIDIA_API_KEY_{n}")) for n in range(1, 6)]
    if missing := [str(n) for n, value in keys if not value]:
        raise SystemExit("Missing NVIDIA_API_KEY_" + ", ".join(missing))

    started = time.time()
    jobs = [(key_n, key, q_n, question) for key_n, key in keys for q_n, question in enumerate(QUESTIONS, 1)]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=200) as pool:
        futures = [pool.submit(ask, *job) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"key {result['key']} / question {result['question']}: {'ok' if result['ok'] else 'FAILED'}", flush=True)

    results.sort(key=lambda item: (item["key"], item["question"]))
    args.output.write_text(json.dumps({"model": MODEL, "questions": QUESTIONS, "results": results}, indent=2), encoding="utf-8")
    successful = sum(result["ok"] for result in results)
    print(f"Completed {successful}/{len(results)} responses in {time.time() - started:.1f}s. Results: {args.output.resolve()}")


if __name__ == "__main__":
    main()
