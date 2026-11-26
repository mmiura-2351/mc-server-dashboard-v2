# Investigation

**You own the answer.** This playbook changes no files and opens no pull
request.

1. Read the report or the question, then the code it is about. Map the symptom
   to a module (`api/`, `worker/`, `relay/`, `webui/`, `proto/`) and a layer
   (`docs/app/ARCHITECTURE.md`).
2. Reproduce it by the cheapest honest route: an existing test, a targeted
   command, a traceback in the report. If it needs a running system that you
   cannot start, say what you verified from the code and what you could not.
3. Pin the cause to a `file:line`. Explain the mechanism in two or three
   sentences: which state or input leads to the wrong behavior. When someone
   says "this used to work", check `git log` and `git blame`.
4. Classify the result:
   - `confirmed-bug`: you found the cause and can show it.
   - `works-as-intended`: the behavior matches the requirements or the docs.
     Cite where.
   - `cannot-reproduce`: say what you tried and what evidence is missing.
   - `needs-decision`: the correct behavior is itself unclear. Give the
     options.
5. If a fix comes next, recommend the approach, the files to touch, the first
   failing test, and whether it fits in one pull request.

**Reply:** the classification, the cause with its `file:line`, the exact
commands you ran with their output, and the recommendation. Mark each claim
`measured`, `inferred`, or `guess`.
