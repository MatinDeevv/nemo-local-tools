"""Five-model, 40-question NVIDIA benchmark. Secrets stay in the supplied .env."""
import argparse
import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
MODELS = {
    1: "nvidia/nemotron-3-ultra-550b-a55b",
    2: "z-ai/glm-5.2",
    3: "openai/gpt-oss-120b",
    4: "nvidia/nemotron-3-super-120b-a12b",
    5: "nvidia/llama-3.3-nemotron-super-49b-v1.5",
}

QUESTIONS = [
"Prove that every finite subgroup of the multiplicative group of a field is cyclic. Keep the proof rigorous and under 500 words.",
"Let X be a compact Riemann surface of genus g>=2. State and derive the Riemann–Roch theorem for a divisor D, explaining the role of the canonical divisor.",
"For f in L1(R), prove or disprove that its Fourier transform is necessarily in L1(R). Give a concrete counterexample or proof.",
"Derive the weak form of the incompressible Navier–Stokes equations on a bounded domain with no-slip boundary conditions, specifying function spaces.",
"Use the spectral theorem to solve dX/dt=AX for a real symmetric matrix A. Explain how the solution changes if A is nonnormal.",
"Compute the fundamental group of the Klein bottle and explain why it is nonabelian.",
"Give a proof sketch of the Central Limit Theorem for iid variables with finite nonzero variance using characteristic functions.",
"Solve the PDE u_t = k u_xx on R with initial condition u(x,0)=exp(-x^2), deriving the closed-form solution.",
"Explain the distinction between uniform convergence and convergence in L2. Give a sequence that converges in L2 but not uniformly.",
"For a convex differentiable function f with L-Lipschitz gradient and mu-strong convexity, derive the linear convergence rate of gradient descent for an appropriate step size.",
"Starting from the Einstein–Hilbert action, derive Einstein's field equations and identify the boundary-term subtlety.",
"Derive the Chandrasekhar mass scaling using relativistic electron degeneracy pressure and gravitational equilibrium; state the physical assumptions.",
"A two-level system has H=(hbar omega/2) sigma_z + (hbar Omega/2) sigma_x. Derive its eigenvalues, eigenstates, and Rabi oscillation probability from |0>.",
"Explain renormalization-group relevance, marginality, and irrelevance near a fixed point, and apply the definitions to phi^4 theory near four dimensions.",
"Derive the dispersion relation for electromagnetic waves in a cold unmagnetized plasma and explain the meaning of plasma frequency.",
"Use Noether's theorem to derive the conserved current associated with global U(1) symmetry of the complex Klein–Gordon field.",
"Derive the ideal-gas adiabatic relation PV^gamma=constant from the first law and equation of state.",
"For a 1D quantum harmonic oscillator, derive the ground-state wavefunction and energy using ladder operators.",
"Explain the Kramers–Kronig relations: assumptions, statement, and why causality implies them.",
"A Schwarzschild black hole has mass M. Derive its Hawking temperature and entropy, keeping constants explicit.",
"Given an unlevered firm with EBIT=100, tax rate 25%, depreciation=20, capex=30, and increase in NWC=10, calculate FCFF. Then explain why FCFF is not net income.",
"Derive the Black–Scholes PDE from delta hedging under geometric Brownian motion. State all modeling assumptions.",
"Explain the Fama–French three-factor model and give two reasons a statistically significant factor premium might not be exploitable.",
"A 10-year zero-coupon bond has yield 4% with annual compounding. Compute modified duration and approximate its percentage price change for a +50 bp yield shift.",
"Compare risk-neutral pricing and real-world expected-return valuation. Show where the risk-neutral measure enters a one-period binomial option valuation.",
"Derive the relationship between WACC, unlevered beta, and relevered beta under the standard Hamada assumptions.",
"Explain adverse selection in credit markets using a simple separating or pooling equilibrium intuition; distinguish it from moral hazard.",
"A portfolio has annual expected return 9%, volatility 15%, risk-free rate 3%, and beta 1.2. Compute its Sharpe ratio and CAPM-implied return if market premium is 5%; interpret the discrepancy.",
"Explain why VaR can fail subadditivity. Give a small discrete-loss counterexample and contrast Expected Shortfall.",
"Describe how to estimate a yield curve from coupon-bond prices using bootstrapping, including how you would handle noisy or inconsistent market quotes.",
"Prove the FLP impossibility result at a high level: define the asynchronous consensus model, bivalence, and the critical event argument.",
"Design a linearizable lock-free stack and explain the ABA problem plus one practical mitigation. Use concise pseudocode.",
"Compare snapshot isolation with serializability. Provide a write-skew schedule that is allowed by snapshot isolation but violates a business invariant.",
"Give a rigorous threat model for a retrieval-augmented generation system facing indirect prompt injection. Propose layered mitigations and residual risks.",
"Derive the Bellman optimality equation for a discounted MDP and explain why the Bellman optimality operator is a contraction.",
"Explain the bias–variance decomposition for squared-error regression, including the irreducible-noise term and its assumptions.",
"For a Transformer attention layer, state the tensor shapes for batch B, sequence length T, hidden size d, and h heads; derive the dominant compute and memory complexity.",
"Explain differential privacy using (epsilon,delta)-DP. Derive the Gaussian mechanism noise scale at a high level and discuss composition.",
"Give a formal comparison of CAP theorem and PACELC. Explain why CAP does not mean a distributed system must choose only two properties in normal operation.",
"Design an evaluation protocol for a coding agent that measures correctness, security regressions, cost, and time-to-merge while preventing benchmark contamination.",
]

def load_env(path):
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        s=raw.strip()
        if s and not s.startswith('#') and '=' in s:
            k,v=s.split('=',1); os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))

def ask(key_number, api_key, question_number, question):
    model=MODELS[key_number]
    payload=json.dumps({"model":model,"messages":[{"role":"system","content":"Answer rigorously but compactly. Show essential derivations. Do not invent citations."},{"role":"user","content":question}],"temperature":0.15,"max_tokens":1400}).encode()
    req=urllib.request.Request(ENDPOINT,data=payload,method='POST',headers={'Authorization':f'Bearer {api_key}','Content-Type':'application/json'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req,timeout=180) as r: body=json.load(r)
            return {'key':key_number,'model':model,'question':question_number,'ok':True,'answer':body['choices'][0]['message'].get('content','')}
        except urllib.error.HTTPError as e:
            detail=e.read().decode('utf-8','replace')[:500]
            if e.code in (429,500,502,503,504) and attempt<3: time.sleep(2**attempt); continue
            return {'key':key_number,'model':model,'question':question_number,'ok':False,'status':e.code,'error':detail}
        except Exception as e:
            if attempt<3: time.sleep(2**attempt); continue
            return {'key':key_number,'model':model,'question':question_number,'ok':False,'error':str(e)}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--env',required=True); p.add_argument('--output',default='nvidia_hard_benchmark_results.json'); a=p.parse_args()
    load_env(a.env)
    keys=[(n,os.environ.get(f'NVIDIA_API_KEY_{n}')) for n in MODELS]
    if any(not v for _,v in keys): raise SystemExit('One or more NVIDIA_API_KEY_1..5 variables are missing.')
    start=time.time(); jobs=[(n,k,q,question) for n,k in keys for q,question in enumerate(QUESTIONS,1)]; results=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=200) as pool:
        futures=[pool.submit(ask,*j) for j in jobs]
        for f in concurrent.futures.as_completed(futures):
            r=f.result(); results.append(r); print(f"{r['key']} {r['model']} Q{r['question']}: {'ok' if r['ok'] else 'FAILED'}",flush=True)
    results.sort(key=lambda x:(x['key'],x['question']))
    Path(a.output).write_text(json.dumps({'models':MODELS,'questions':QUESTIONS,'results':results},indent=2),encoding='utf-8')
    print(f"Completed {sum(r['ok'] for r in results)}/{len(results)} in {time.time()-start:.1f}s. Results: {Path(a.output).resolve()}")
if __name__=='__main__': main()
