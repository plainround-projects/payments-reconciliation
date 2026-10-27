# The shell (sh and Bash)

## What it is

A **shell** is a program that reads a line of text, treats the first word as a program to run and the other words as its arguments, and starts it. It also has a small language of its own: pipes, redirection, variables, loops and tests. Bash is the common shell on Linux; `sh` is the simpler standard one that `make` uses to run recipes. This repository has no stand-alone `.sh` file, but shell syntax appears in the Makefile, the Compose health check and the CI workflow, so a reader needs to follow it.

## Why this repository uses it

Starting a database, loading SQL files into it and checking it is healthy are all "run a program and look at its exit status" jobs. The shell is the glue that does this in a few words.

## Where it appears

| Path | Shell used |
| --- | --- |
| `Makefile` | Every recipe line is run by `/bin/sh -c`: the `[ -f .env ] || { ... }` test in `env`, redirection `< sql/001_schema.sql`, the `for` loop in `migrate`, the pipe in `help`. |
| `docker-compose.yml` | The health check `["CMD-SHELL", "pg_isready ... $${POSTGRES_USER}"]` is run through a shell inside the container. |
| `.github/workflows/ci.yml` | Each `run:` line is a shell command on the CI machine. |

## How it works underneath

- Every program ends with an **exit status**, a number. `0` means success, anything else means failure. The shell stores the last status in `$?`.
- `A && B` runs B only if A succeeded. `A || B` runs B only if A failed. `A; B` runs both.
- **Standard input** (stdin) is where a program reads, **standard output** (stdout) is where it writes, and **standard error** (stderr) is for messages about problems. `<` feeds a file into stdin. `>` sends stdout to a file. `|` connects one program's stdout to the next one's stdin.
- Before running a command, the shell expands variables (`$NAME`), patterns (`*.sql`) and quotes.

## Walkthrough of this repository's use

### Creating `.env` only once

```make
env:
	@[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }
```

- `[ -f .env ]` is the `test` command: true when `.env` is a regular file. The spaces inside the brackets are required.
- `||` means: if the file does **not** exist (the test failed), run what follows.
- `{ ...; ...; }` groups two commands. `cp` copies the example, `echo` prints a note. The semicolon before `}` is required.
- The leading `@` is make's, not the shell's: it hides the command line.

### Feeding SQL files to PostgreSQL

```make
PSQL := docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q
...
	$(PSQL) < sql/001_schema.sql
```

- `docker compose exec -T postgres psql ...` runs the `psql` client inside the running container. `-T` turns off the pseudo-terminal, which is needed when stdin is a file.
- `< sql/001_schema.sql` opens the file on the host and connects it to the program's stdin, so `psql` reads the SQL from it.
- `-v ON_ERROR_STOP=1` is a psql variable: stop at the first error and exit non-zero, so `make` notices a broken script.
- `-q` is quiet.

### A loop over files

```make
	@for file in sql/migrations/*.sql; do echo "applying $$file"; \
	  docker compose exec -T postgres psql -U $(POSTGRES_USER) -d reconciliation_migrated -v ON_ERROR_STOP=1 -q < $$file; done
```

- `sql/migrations/*.sql` is a **glob**; the shell replaces it with the sorted list of matching names (`001_payments_and_ledger.sql`, `002_reconciliation.sql`, `003_resolutions.sql`). The numeric prefixes make sorted order equal to the order to apply.
- `for file in LIST; do ...; done` repeats the body for each name.
- `$$file` is make's way to write `$file`. The trailing `\` continues the line.

### Preparing files before the program runs

```make
run: $(STAMP) db
	rm -rf var/settlement
	$(CLI) demo

reconcile-sample: $(STAMP)
	mkdir -p var/settlement && cp data/settlement_2026-10-19.csv var/settlement/
	$(CLI) reconcile --date 2026-10-19
```

- `rm -rf var/settlement` deletes the folder and everything in it (`-r` recursive, `-f` no questions, no error if it is missing). It makes every `make run` start with no settlement files.
- `mkdir -p var/settlement` creates the folder; `-p` creates parents as needed and does not complain if it exists.
- `&&` runs `cp` only if `mkdir` succeeded. `cp file folder/` copies the checked-in sample CSV into the folder the program reads from.

### A health check through a shell

```yaml
test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
```

`CMD-SHELL` hands the string to `/bin/sh -c` inside the container, so `${POSTGRES_USER}` is read from that container's environment. The doubled `$$` stops Docker Compose from filling the variable in itself while reading the YAML. `pg_isready` exits `0` when the server accepts connections. `-h 127.0.0.1` forces a network connection, because during first start the database briefly runs a temporary server that only listens on a socket; without it the check can report healthy too early.

### A pipe in `help`

```make
@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort
```

Three programs in a row: `grep` selects lines, `sed` rewrites them, `sort` orders them. `-E` means extended regular expressions; `^` is "start of line"; `[a-z-]+` is one or more lowercase letters or hyphens.

### Quoting

Single quotes (`'...'`) keep every character as it is. Double quotes (`"..."`) still expand `$VAR`. The repository uses single quotes around regular expressions so the shell does not touch `$`, `*` and `^`.

## Try it yourself

1. Exit statuses:

   ```bash
   true; echo $?; false; echo $?
   ```

   Expected: `0` then `1`.

2. The `||` idiom:

   ```bash
   [ -f /nope ] || echo "missing"
   ```

   Expected: `missing`.

3. A glob in the order the migrations run:

   ```bash
   echo sql/migrations/*.sql
   ```

   Expected: `sql/migrations/001_payments_and_ledger.sql sql/migrations/002_reconciliation.sql sql/migrations/003_resolutions.sql`.

4. A pipe, the same shape as `help`:

   ```bash
   make help | sort | head -3
   ```

   Expected: the first three target names with descriptions.

## Mistakes people make

- **Unquoted variables with spaces.** `rm $file` splits on spaces; write `rm "$file"`.
- **Forgetting that `||` runs on failure.** `[ -f .env ] || cp ...` copies when the file is **missing**.
- **Using `$name` in a Makefile for a shell variable.** Write `$$name`.
- **Ignoring failures in a loop.** In `migrate`, `-v ON_ERROR_STOP=1` makes psql exit non-zero so the loop's failing command stops make.
- **Assuming Bash features inside `sh`.** Make uses `/bin/sh`; this repository sticks to plain POSIX syntax (no arrays, no `[[ ]]`).

## Check yourself

1. What does an exit status of `0` mean? *Success.*
2. What does `< file` do? *It makes the program read the file as its standard input.*
3. Why `$${POSTGRES_USER}` in the Compose file? *`$$` escapes Compose's own substitution so the container's shell sees `${POSTGRES_USER}`.*
4. What does `A || B` do? *Runs B only if A failed.*
5. Why does the migration loop apply files in the right order? *The shell expands the glob in sorted order and the filenames start with 001, 002, ...*

## Further reading

- POSIX shell command language (what `sh` implements): https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap02.html
- GNU Bash manual (macOS and Linux Bash 3.2 or newer): https://www.gnu.org/software/bash/manual/
- `psql` options (PostgreSQL 18): https://www.postgresql.org/docs/18/app-psql.html
- `pg_isready`: https://www.postgresql.org/docs/18/app-pg-isready.html
