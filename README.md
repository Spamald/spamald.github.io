# Safe File Agent

A terminal AI agent that turns natural-language requests into validated local
file operations. The design follows the lecture's LLM → action interpretation
→ execution flow, with the action set constrained to file management.

## Requirements

- Python 3.10+
- A Groq API key (the LLM integration uses Groq's OpenAI-compatible chat
  completions API)

## Run

Set the API key in PowerShell, then launch the program from the directory the
agent should manage. To try it safely with the included sample files, use
`test_files` as the managed root:

```powershell
$secret = Read-Host "Groq API key" -AsSecureString
$env:GROQ_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
python -m agent.cli --root ".\test_files"
```

The sample folder includes files you can list, inspect, rename, move, or remove.
Confirmations are still required for changes. The unit test suite is separate
and can be run with the command under [Tests](#tests).

## File-manager DSL

The agent also accepts a simple script with one command per line. Commands run
in order, paths are relative to the selected managed root, and paths with
spaces must be quoted. Blank lines and lines beginning with `#` are ignored.
The entire script is parsed and validated before any command runs. If a
command fails or a write is declined, execution stops; prior completed
commands are not rolled back.

| Command | File-manager operation |
| --- | --- |
| `LIST [path]` | List a directory and show its full path; defaults to the managed root |
| `INFO <file>` | Show a file's name and size |
| `CREATE_FILE <path>` | Create a new empty file; its parent folder must exist |
| `CREATE_FOLDER <path>` | Create a new folder, including missing parent folders |
| `RENAME_FILE <file> TO <new-name>` | Rename a file |
| `RENAME_FOLDER <folder> TO <new-name>` | Rename a folder |
| `MOVE_FILE <file> TO <destination>` | Move a file to a path or existing folder |
| `REMOVE_FILE <file>` | Remove a file |

Example `commands.fma` (also included in the project root):

```text
# Inspect the sample root and a file
LIST
INFO "readme.txt"

# Creation commands also pause for confirmation
CREATE_FOLDER "demo_folder/A4"
CREATE_FILE "demo_folder/A4/notes.txt"
```

Run a script without an LLM API key:

```powershell
python -m agent.cli --root ".\test_files" --dsl ".\commands.fma"
```

Each write command still requires typing `yes`. The same path confinement,
no-overwrite protections, and audit log used by the interactive agent apply.

## Automatic organization

Start the terminal workflow with:

```powershell
python -m agent.cli --organize
```

Enter the folder path when prompted. The program recursively lists it using
the file manager and displays file/folder metadata (names, relative and full
paths, extensions, sizes, and modification times). It asks before sending
metadata to Groq; file contents are never read or sent. Groq returns a DSL
plan, which is displayed and checked to allow only `CREATE_FOLDER` and
`MOVE_FILE`, with every inventoried file moved exactly once. Confirm each
filesystem change individually. Declining a confirmation or encountering an
error stops the plan; already completed operations are not rolled back.
Organization is based on filenames and extensions, not file contents.

Optional configuration:

```powershell
$env:GROQ_MODEL = "openai/gpt-oss-20b"
$env:GROQ_API_BASE = "https://api.groq.com/openai/v1"
python -m agent.cli --root "C:\path\to\managed-files"
```

Enter a natural-language request at the prompt. Type `exit` or `quit` to end
the session. The agent supports:

- Listing a directory's entries, sorted by name
- Getting a file's name and size
- Creating an empty file or folder
- Renaming a file or folder
- Moving a file
- Removing a file

When listing without naming a subfolder, the agent uses the managed root.
The listing result includes `base_folder`, the absolute path of the folder
whose contents were listed. Paths are relative to the managed root, so you do
not need to repeat the root folder name. File actions can use a filename without its extension if it matches one
file unambiguously. Source file names can also be found in nested folders when
the name is unique. For a move, choosing an existing folder as the destination
moves the file into that folder while keeping its name; an existing file is
never overwritten.

Every write operation requires the user to type `yes` before it is performed.
All paths are confined to the selected root, existing symbolic links are
rejected, and file removal cannot target a directory. The agent does not run
shell commands or execute arbitrary LLM-generated code.

## Audit log

Each request, proposed operation, confirmation decision, and execution result
is appended to `~/.ai-file-agent/audit.jsonl`. Set `AGENT_AUDIT_LOG` to choose
another location. The log contains the user's request text; store it somewhere
appropriate for your privacy needs.

## Tests

Run the test suite with:

```powershell
python -m unittest discover -s tests -v
```
