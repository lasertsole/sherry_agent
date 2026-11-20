# CyberSecEval-style suite

Security multiple-choice / code analysis, host mode. The plan's own
assessment: this benchmark family is mostly MCQ — no container interaction.
One dataset file (`CYBERSEC_DATA_DIR` or `--dataset`): JSONL/JSON of
`{task_id, question, choices: [...], answer: "A", category?}`. The agent
answers with a reasoning turn whose LAST line is the choice letter; scoring is
the letter match (`evals/cybersec/suite.py::extract_choice` takes the last
bare letter).

SecBench records with the same shape run in this suite unchanged.

## Run

```bash
uv run python evals/evals.py cybersec --dry-run
uv run python evals/evals.py cybersec --all --batch 0-199
```
