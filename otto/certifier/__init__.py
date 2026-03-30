"""Otto product certifier — independent, evidence-based product evaluation.

Evaluates any software product against its original intent. Builder-blind:
doesn't know if otto, bare CC, or a human built it.

Architecture:
  Intent → Intent Compiler → Requirement Matrix
                                    ↓
                          Product Classifier
                                    ↓
                          Deterministic Baseline (Tier 1)
                                    ↓
                          Exploratory Workers (Tier 2)
                                    ↓
                          Judge → Certification Report
"""
