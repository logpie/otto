# State of the Art: LLM-Based Test Generation & Rubric/Spec Generation for Autonomous Coding Agents

Research conducted 2026-03-14. Covers published papers, tools, and techniques through early 2026.

---

## 1. Multi-Pass Rubric Refinement

### 1.1 RRD: Recursive Decompose-Filter for Rubric Refinement
- **Source**: [Rethinking Rubric Generation for Improving LLM Judge and Reward Modeling](https://arxiv.org/abs/2602.05125) (Shen et al., Feb 2026)
- **Core idea**: Coarse rubrics are recursively decomposed into fine-grained, discriminative criteria via a decompose-filter cycle. The decomposition expands coverage; the filter removes misaligned, redundant, or correlated criteria. This is iterated until rubric quality stabilizes.
- **Results**: +17.7 points on JudgeBench for preference-judgment accuracy. When used as reward source for reinforcement fine-tuning, yields 60-160% stronger learning signals vs. prior rubric baselines.
- **Applicability to Otto**: Directly applicable. When generating acceptance criteria from a task description, run a decompose pass (break high-level criteria into specific, testable sub-criteria), then a filter pass (remove redundant/correlated criteria, check alignment with task intent). Iterate 2-3 rounds. This is the closest published work to what Otto needs for rubric generation.

### 1.2 Reflect-and-Revise for Rubric Refinement
- **Source**: [Automated Refinement of Essay Scoring Rubrics for Language Models via Reflect-and-Revise](https://arxiv.org/abs/2510.09030) (Oct 2025)
- **Core idea**: LLMs iteratively refine rubrics by reflecting on their own scoring rationales and observed score discrepancies with ground-truth scores. Starting from even a minimal rubric ("rate on a scale of 1 to 6"), the LLM converges to rubrics that match or beat expert-authored rubrics. Inspired by prompt optimization.
- **Results**: QWK improvements of 0.19 (TOEFL11) and 0.47 (ASAP). LLMs can autonomously identify relevant evaluation criteria from minimal starting points.
- **Applicability to Otto**: The "reflect on discrepancies" loop is powerful. After generating rubric criteria, have the LLM score example outputs against the rubric, then refine criteria where scoring rationale is ambiguous or inconsistent. Even starting with a rough task description, the model can converge on meaningful criteria.

### 1.3 CARMO: Dynamic Context-Aware Criteria Generation
- **Source**: [CARMO: Dynamic Criteria Generation for Context-Aware Reward Modelling](https://aclanthology.org/2025.findings-acl.114/) (Microsoft Research, ACL 2025 Findings)
- **Core idea**: Instead of static rubrics, generate per-prompt evaluation criteria dynamically. The LLM first generates criteria (logical consistency, clarity, depth, etc.) tailored to the specific query, then scores responses against those criteria. Theoretical analysis shows this mitigates reward hacking.
- **Results**: New SOTA on Reward Bench (zero-shot). Can be distilled into smaller models.
- **Applicability to Otto**: Generate task-specific rubric criteria per task rather than using a fixed rubric template. For a "build a REST API" task, generate criteria about endpoint correctness, error handling, status codes. For an "implement sorting algorithm," generate criteria about correctness, edge cases, complexity. The criteria should be dynamic, not generic.

### 1.4 Self-Refine (Foundational)
- **Source**: [Self-Refine: Iterative Refinement with Self-Feedback](https://arxiv.org/abs/2303.17651) (Madaan et al., 2023, widely cited through 2025)
- **Core idea**: Single LLM acts as generator, critic, and refiner. Generate output, critique it, refine based on critique. No training required. Known limitation: self-bias (LLMs systematically overrate their own generations).
- **Applicability to Otto**: Baseline approach. Use for initial rubric generation, but augment with external signals (mutation testing results, coverage data) to break self-bias. Self-Refine alone is insufficient for high-quality rubrics.

---

## 2. Spec-to-Test Coverage Verification

### 2.1 LLM-as-a-Judge for Test Coverage Evaluation (LAJ)
- **Source**: [LLM-as-a-Judge for Scalable Test Coverage Evaluation](https://arxiv.org/abs/2512.01232) (Huang et al., Dec 2025)
- **Core idea**: Rubric-driven LLM framework for evaluating Gherkin acceptance tests. Given a spec and generated tests, the LLM judges whether tests cover the spec using structured JSON outputs. Introduces Evaluation Completion Rate (ECR@1) metric. Tested 20 model configurations on 100 expert-annotated scripts.
- **Results**: Reliability 85.4%-100.0% on first-attempt evaluations. GPT-5 configurations most reliable.
- **Applicability to Otto**: After generating tests from rubric criteria, use a separate LLM call to evaluate "does this test suite cover criterion X?" for each criterion. Produces a traceability matrix (criterion -> test mapping) as a side effect. Cheap and effective.

### 2.2 Multi-Step Test Specification Generation
- **Source**: [Multi-Step Generation of Test Specifications using Large Language Models](https://aclanthology.org/2025.acl-industry.11.pdf) (Milchevski et al., ACL 2025 Industry Track)
- **Core idea**: Multi-step pipeline: (1) generate test purposes/scenarios from requirements, (2) enhance via multi-agent approach, (3) retrieve relevant existing specs for consistency. Evaluated with BERTScore, ROUGE-L, and LLM-as-a-Judge. Designed for automotive ISO 26262 compliance but generalizable.
- **Applicability to Otto**: The multi-step decomposition (requirements -> scenarios -> test specs) maps well to Otto's flow (task description -> rubric criteria -> test cases). The retrieval component is useful for maintaining consistency with existing tests.

### 2.3 Generating High-Level Test Cases from Requirements
- **Source**: [Generating High-Level Test Cases from Requirements using LLM: An Industry Study](https://arxiv.org/abs/2510.03641) (Masuda et al., Oct 2025)
- **Core idea**: Prompt-only approach (no RAG). Input requirement document, generate test design techniques per requirement, then generate test cases per technique. Evaluated on Bluetooth and Mozilla Firefox specs.
- **Results**: Macro-recall of 0.81 (Bluetooth) and 0.37 (Mozilla). The two-stage approach (first identify test design techniques, then generate cases) outperforms direct generation.
- **Applicability to Otto**: The two-stage approach is directly useful. Don't generate tests directly from rubric criteria. First, for each criterion, identify what testing strategies apply (boundary values, equivalence partitioning, error scenarios, happy path). Then generate concrete tests per strategy. This increases coverage.

### 2.4 TestCase-Eval Benchmark
- **Source**: [TestCase-Eval: A Systematic Evaluation of Fault Coverage and Exposure](https://arxiv.org/abs/2506.12278) (ACL 2025)
- **Core idea**: Benchmark with 500 algorithm problems + 100K human solutions from Codeforces. Evaluates LLM test generation on Fault Coverage (diversity of failure modes covered) and Fault Exposure (ability to craft inputs revealing specific bugs). Best LLM (Qwen3-32B) achieves 43.8% fault exposure vs. humans at 93.3%.
- **Applicability to Otto**: Sobering calibration data. LLMs are still far from human-level at crafting targeted test inputs. Implies Otto should not rely solely on LLM-generated tests; mutation testing and property-based testing are essential supplements.

---

## 3. Test Quality Self-Assessment

### 3.1 Meta ACH: Mutation-Guided LLM Test Generation
- **Source**: [Mutation-Guided LLM-based Test Generation at Meta](https://arxiv.org/html/2501.12862v1) + [Engineering blog post](https://engineering.fb.com/2025/09/30/security/llms-are-the-key-to-mutation-testing-and-better-compliance/) (Meta, FSE 2025)
- **Core idea**: Three-agent architecture: (1) Fault Generator introduces simulated faults/mutants into code, (2) Equivalence Detector filters semantically-equivalent mutants, (3) Test Generator creates tests that catch the faults. Uses Llama 3.1 70B. Deployed across Facebook, Instagram, WhatsApp, Messenger.
- **Results**: 9,095 mutants generated across 10,795 Kotlin classes, 571 hardening test cases, 73% acceptance rate by engineers, 36% judged as genuinely privacy-relevant.
- **Applicability to Otto**: The three-agent decomposition is clean and directly implementable. Otto already has mutation testing; the key insight is using the LLM to generate *targeted* mutants (not random ones) based on the specific rubric criteria, then generating tests that must kill those mutants. The equivalence detector is important to avoid wasting time on trivial/equivalent mutants.

### 3.2 MuTAP: Mutation-Augmented Prompting
- **Source**: [Effective Test Generation Using Pre-trained LLMs and Mutation Testing](https://arxiv.org/abs/2308.16557) (IST 2024, widely referenced 2025)
- **Core idea**: Iterative loop: (1) generate initial tests, (2) run mutation testing, (3) identify surviving mutants, (4) re-prompt the LLM with both the initial tests AND the surviving mutants, asking for new tests that kill the survivors. Repeat until no new surviving mutants or convergence.
- **Results**: Detects 28% more faulty code snippets. Mutation score of 93.57% on synthetic bugs.
- **Applicability to Otto**: The most directly applicable technique for Otto's existing mutation testing. After initial test generation, run mutants, feed survivors back as prompt context, and iterate. Simple to implement, proven effective. The key is formatting surviving mutants clearly in the prompt.

### 3.3 LLMorpheus: LLM-Based Realistic Mutant Generation
- **Source**: [LLMorpheus: Mutation Testing using Large Language Models](https://arxiv.org/abs/2404.09952) (GitHub Next, TSE 2025)
- **Core idea**: Instead of traditional mutation operators (swap +/-), use an LLM to generate realistic, developer-like mutants. Replace code fragments with PLACEHOLDER tokens, prompt LLM to suggest replacements. Produces bugs that resemble real developer mistakes, unlike traditional mutation operators.
- **Results**: Generates mutants that cannot be produced by StrykerJS (traditional tool). More realistic fault models.
- **Applicability to Otto**: Use LLM-generated mutants instead of (or in addition to) traditional AST-based mutations. More realistic mutants mean tests that kill them are more likely to catch real bugs. Feed task description as context so mutants are domain-relevant.

### 3.4 Test Smell Detection in LLM-Generated Tests
- **Source**: [Test Smells in LLM-Generated Unit Tests](https://arxiv.org/abs/2410.10628) (Oct 2024, widely cited 2025)
- **Core idea**: Large-scale analysis of 20,500 LLM-generated test suites (GPT-3.5, GPT-4, Mistral 7B, Mixtral 8x7B) across 5 prompt techniques, compared with 780,144 human-written suites. LLM tests consistently exhibit Assertion Roulette (multiple assertions without messages), Magic Number Test (unexplained constants), and other smells. Prompting strategy has stronger impact than model choice.
- **Applicability to Otto**: Post-generation quality gate. After generating tests, run automated smell detection. Flag and regenerate tests with Assertion Roulette (indicates the test isn't testing anything specific), empty tests, conditional test logic, or magic numbers. This is cheap and catches the most common LLM test failure modes.

### 3.5 Test Oracle Problem
- **Source**: [Understanding LLM-Driven Test Oracle Generation](https://arxiv.org/abs/2601.05542) (Jan 2026)
- **Core idea**: LLM-generated test oracles primarily capture regression behavior (what the code does) rather than specification behavior (what the code should do). Prompt design matters more than model choice for oracle quality. This is the fundamental problem: tests that pass on current implementation aren't necessarily correct tests.
- **Applicability to Otto**: Critical insight. When generating tests from rubric criteria, the rubric must be the oracle source, not the implementation. Generate expected behaviors from the spec/rubric BEFORE seeing the implementation. Tests should be specification-driven, not implementation-driven.

---

## 4. Black-Box Test Generation from Specifications

### 4.1 Property-Generated Solver (PGS)
- **Source**: [Use Property-Based Testing to Bridge LLM Code Generation and Validation](https://arxiv.org/abs/2506.18315) (Jun 2025)
- **Core idea**: Two-agent framework: Generator (code generation + iterative refinement) and Tester (manages PBT lifecycle, formulates semantic feedback from property violations). Tests properties/invariants rather than specific input-output pairs. Breaks the "cycle of self-deception" where tests share flaws with code.
- **Results**: 23.1%-37.3% pass@1 improvements over TDD methods.
- **Applicability to Otto**: Highly relevant. Instead of generating example-based tests (assertEqual(foo(3), 9)), generate property-based tests (for all x, foo(x) >= 0, foo(x) == foo(-x), etc.). Properties are easier to derive from specs than exact expected outputs, and they generalize better. The Tester agent providing semantic feedback from property violations is directly applicable.

### 4.2 LLM-Based Property-Based Test Generation for CPS
- **Source**: [LLM-based Property-based Test Generation for Guardrailing Cyber-Physical Systems](https://arxiv.org/html/2505.23549) (May 2025)
- **Core idea**: Extract properties from code + documentation using LLM, then generate property-based tests that verify those properties. Implemented as ChekProp (open source). Properties extracted include invariants, preconditions, postconditions, and behavioral properties.
- **Applicability to Otto**: The property extraction step is useful. From rubric criteria, extract testable properties (invariants, pre/postconditions) using the LLM, then generate Hypothesis-based PBT code. This is more robust than example-based tests.

### 4.3 Validating Formal Specifications with LLM-Generated Tests
- **Source**: [Validating Formal Specifications with LLM-generated Test Cases](https://arxiv.org/html/2510.23350v1) (FM 2026)
- **Core idea**: Use LLMs to generate test cases from natural-language requirements, then use those tests to validate formal specifications. The tests serve as a bridge between informal requirements and formal specs.
- **Applicability to Otto**: Interesting inversion. Instead of testing code against specs, test specs against requirements. Could be used to validate that Otto's generated rubric criteria actually capture the task intent.

### 4.4 Boundary Value Test Input Generation
- **Source**: [Boundary Value Test Input Generation Using a Large Language Model](https://link.springer.com/chapter/10.1007/978-981-95-3459-3_32) (2026)
- **Core idea**: LLM generates boundary value test inputs with fault detection and coverage analysis. Focuses on edge cases that are often missed by random or example-based generation.
- **Applicability to Otto**: Supplement standard test generation with explicit boundary-value prompting. Ask the LLM specifically for edge cases, boundary conditions, and corner cases per rubric criterion.

### 4.5 HITS: Method Slicing for High-Coverage Test Generation
- **Source**: [HITS: High-coverage LLM-based Unit Test Generation via Method Slicing](https://arxiv.org/abs/2408.11324) (ASE 2024, widely cited 2025)
- **Core idea**: Decompose complex methods into slices (logical segments, execution paths), generate tests per slice using Chain-of-Thought decomposition. Retrieves context (dependent classes, fields, Javadocs) for each slice. 10-20% better coverage than whole-method approaches.
- **Applicability to Otto**: When generating tests for complex implementations, decompose the target code into logical slices before generating tests. This is an implementation-aware technique (not purely black-box), but useful for the test generation phase after code exists.

---

## 5. Multi-Agent Adversarial Setups

### 5.1 AdverTest: Test vs. Mutant
- **Source**: [Test vs Mutant: Adversarial LLM Agents for Robust Unit Test Generation](https://arxiv.org/abs/2602.08146) (Feb 2026)
- **Core idea**: Two LLM agents in adversarial loop. Mutant Agent generates mutants targeting blind spots of current test suite. Test Agent generates/refines tests to kill the mutants. Bidirectional feedback via mutation score and coverage. The mutant agent learns to "hack" the test agent's weaknesses; the test agent adapts. Co-evolution drives both to improve.
- **Results**: 66.63% FDR on Defects4J (8.56% over HITS, 63.3% over EvoSuite). Using DeepSeek V3.2.
- **Applicability to Otto**: The most directly applicable adversarial setup. Implement as two separate LLM calls: (1) "Given this code and these tests, generate mutations the tests won't catch," (2) "Given these surviving mutations, generate tests that catch them." Iterate until mutation score plateaus. This is the adversarial refinement loop Otto needs.

### 5.2 UTRL: Adversarial RL for Test Generation
- **Source**: [Learning to Generate Unit Tests via Adversarial Reinforcement Learning](https://arxiv.org/abs/2508.21107) (Aug 2025)
- **Core idea**: Train test generator LLM and code generator LLM adversarially via RL. Test generator maximizes discrimination reward (ability to distinguish LLM-generated code from ground-truth). Code generator maximizes pass rate against generated tests. Co-evolution: code generator produces increasingly subtle bugs; test generator produces increasingly discriminative tests.
- **Results**: Qwen3-4B trained via UTRL outperforms GPT-4.1 at generating discriminative unit tests.
- **Applicability to Otto**: Training-time technique, not directly usable at inference. But the insight is valuable: the best test is one that discriminates between correct and nearly-correct implementations. Frame test quality as "can this test distinguish the correct implementation from a plausible-but-wrong one?"

### 5.3 ATGen: Adversarial RL with Curriculum
- **Source**: [ATGen: Adversarial Reinforcement Learning for Test Case Generation](https://arxiv.org/abs/2510.14635) (ICLR 2026 Poster)
- **Core idea**: Test generator trained via RL against adversarial code generator that crafts increasingly harder bugs. Creates a curriculum of escalating difficulty, breaking the "fixed-difficulty ceiling" of static training data. Optimizes both Output Accuracy and Attack Success.
- **Results**: Qwen2.5-7B achieves 60% relative improvement in Attack Rate over GPT-4-turbo, >2x as effective at finding bugs vs. UTGen (36.99% vs 16.24%).
- **Applicability to Otto**: Like UTRL, this is a training-time approach. But the curriculum idea is usable at inference: start with easy mutants, escalate difficulty, use progressively harder mutants to push test quality up.

### 5.4 DebateCoder: Test-Driven LLM Debate
- **Source**: [DebateCoder: Towards Collective Intelligence of LLMs via Test Case Driven LLM Debate for Code Generation](https://aclanthology.org/2025.acl-long.589/) (ACL 2025)
- **Core idea**: Two LLM models debate about code correctness. Test cases serve as the medium: each model generates test cases to challenge the other's code. Execution results drive contrastive analysis and code refinement. Five-stage pipeline with convergence criteria based on test outcomes.
- **Applicability to Otto**: The debate-as-test-driven-development concept is directly applicable. Have one LLM generate code, another generate adversarial tests. Run tests, feed results back. The "contrastive analysis" step (comparing why one solution passes and another fails) produces useful debugging signal.

### 5.5 Multi-Agent Debate for Requirements Engineering
- **Source**: [Multi-Agent Debate Strategies to Enhance Requirements Engineering with Large Language Models](https://arxiv.org/abs/2507.05981) (Jul 2025)
- **Core idea**: Applies multi-agent debate (MAD) specifically to requirements engineering tasks. Multiple LLM agents debate requirements completeness, consistency, and correctness.
- **Applicability to Otto**: Could be applied to rubric generation: have multiple agents debate whether rubric criteria are complete, non-overlapping, and testable. Catches gaps that single-pass generation misses.

---

## Synthesis: Recommended Architecture for Otto

Based on this research, the highest-impact techniques for Otto (an autonomous coding agent runner with rubric-based evaluation) are:

### Phase 1: Rubric Generation
1. **Dynamic criteria generation** (CARMO-style) — generate task-specific criteria, not generic ones
2. **Recursive decompose-filter** (RRD-style) — decompose coarse criteria into fine-grained, testable sub-criteria; filter redundant/misaligned ones
3. **Multi-agent debate** for rubric completeness — have a second LLM challenge whether criteria are complete and non-overlapping

### Phase 2: Test Generation
4. **Two-stage generation** (Masuda et al.) — for each criterion, first identify test strategies (boundary, equivalence, error, happy path), then generate concrete tests per strategy
5. **Property-based test generation** (PGS-style) — generate invariants/properties from criteria, not just example-based tests
6. **Method slicing** (HITS-style) — decompose complex targets before generating tests

### Phase 3: Test Quality Verification
7. **Mutation-augmented iteration** (MuTAP-style) — run mutation testing, feed surviving mutants back into prompt, regenerate tests. Iterate until convergence
8. **LLM-based realistic mutants** (LLMorpheus-style) — generate mutants that look like real developer mistakes, not just AST swaps
9. **Adversarial dual-agent loop** (AdverTest-style) — mutant agent vs. test agent in co-evolution loop
10. **Test smell detection** — post-generation quality gate for Assertion Roulette, Magic Numbers, empty tests

### Phase 4: Coverage Verification
11. **LLM-as-Judge coverage evaluation** (LAJ-style) — for each rubric criterion, judge whether the test suite covers it
12. **Spec-first oracle generation** — derive expected behaviors from rubric BEFORE seeing implementation, not after (avoids regression-oracle trap)

### Key Insight from Literature
The test oracle problem is the fundamental challenge. LLM-generated tests tend to test what the code does, not what it should do. The rubric/spec must be the oracle source, and tests should be generated from the spec independently of the implementation. This is exactly what Otto's rubric-first architecture enables, and it's where Otto has a structural advantage over tools that generate tests from code.
