# harbor-inspect

Read [Harbor](https://github.com/harbor-framework/harbor) tasks without running them.

Harbor's own CLI is the tool for running, linting and publishing tasks. This is a small read-only companion for the moment before that, when someone hands you a folder of tasks and you want to know what is in it. One file, standard library only, Python 3.11+.

The format itself is walked through in [The Harbor task format, file by file](https://nealdes.ai/articles/harbor-format).

```bash
python harbor_inspect.py anatomy path/to/task        # which file is for whom
python harbor_inspect.py census  path/to/dataset     # what the set looks like in aggregate
python harbor_inspect.py flags   path/to/dataset --explain
```

## anatomy

Shows each part of a task, who reads it, and when.

```text
terminal-bench/db-wal-recovery

  task.toml                    harness              schema 1.1; agent timeout 900.0s; verifier shared; network public
  instruction.md               agent                117 words, passed as the prompt and never written into the sandbox
  environment/                 agent sandbox        25,111 bytes; image from alexgshaw/db-wal-recovery:20251031, ubuntu:24.04
  tests/                       verifier only        4,871 bytes; runs in agent container AFTER the agent finishes, in the same container
    tests/test.sh              verifier entrypoint  must write /logs/verifier/reward.txt
  solution/                    oracle agent only    3,080 bytes; copied to /solution and run by `-a oracle`

  flags: shared-verifier, verifier-installs-at-test-time, unpinned-image, root-agent
```

## census

Aggregates every `task.toml` under one or more directories: schema versions, verifier mode, network mode, image pinning, solutions, timeouts, and how many tasks carry each flag. Add `--json` for machine-readable output and `--tasks` to include the per-task records.

## flags

Static signals that a task is hard to reproduce or easier to game. They are heuristics read from files, so treat a flag as a question to ask, not a verdict.

| Flag | What it means |
| --- | --- |
| `shared-verifier` | The verifier runs in the agent's container, so the agent's files and processes are in reach of the grader. |
| `verifier-installs-at-test-time` | `test.sh` installs packages while grading. A network or index failure becomes a reward of zero. |
| `verifier-needs-secret` | Grading needs a host API key, which usually means a model judge. |
| `unpinned-image` | A base image is referenced by tag, not digest. |
| `image-not-in-task` | The Dockerfile builds on an image the task does not define or pin. |
| `host-dependency` | The task talks to a service on the host machine that the task directory does not start. |
| `no-solution` | No `solution/solve.sh`, so the oracle agent cannot confirm the task is solvable. |
| `no-reward-write` | Nothing under `tests/` mentions `reward.txt` or `reward.json`. |
| `no-agent-timeout` | No agent timeout is set. |
| `root-agent` | The agent runs as root. |
| `answer-like-file-in-environment` | A file in `environment/` has a name that suggests an answer key. |
| `no-canary` | No canary string anywhere in the task. |
| `build-context-collision` | A file that a Docker build copies in has the same path and size as a different file in another scanned task. |

`harbor lint` covers several of these and more, with configuration and Compose checks. Run it as well. The flags here that it does not report are `verifier-needs-secret`, `image-not-in-task`, `host-dependency`, `no-solution`, `no-reward-write` and `no-canary`. `build-context-collision` is checked across every task in the scan, so it only appears when more than one task is given.

### Why `build-context-collision` exists

Docker's builder can reuse a file from an earlier build whose context folder has the same name when the file's path, size and timestamp all match. Harbor tasks all call their folders `environment/` and `tests/`, and a separate verifier builds `tests/` as its own image, so two tasks that each hold a `tests/expected.json` of the same size can be built with each other's file once their timestamps agree. Unpacking an archive makes that likely because it sets whole-second timestamps. I hit this with Docker 28.0.4 and buildx 0.22.0 on macOS: an oracle run wrote the right answer and was graded against another task's key. Giving per-task files distinct names (a content hash works) removes the problem. The case is written up in section 4.4 of [this report](https://nealdes.ai/assets/orbifex/orbifex-paper.pdf) and illustrated in [the article](https://nealdes.ai/articles/persona-to-environment#fault).

## What it does not do

It never builds an image, starts a container or calls a model, so it cannot tell you whether a task is solvable or whether its verifier is correct. For that, run the oracle and no-op agents:

```bash
harbor run -p path/to/dataset -a oracle    # every reward should be 1
harbor run -p path/to/dataset -a nop       # every reward should be 0
```

Not affiliated with the Harbor project. Written against Harbor 0.26.0 and task schema 1.4.
