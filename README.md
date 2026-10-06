# Codex Context Editor

A small local web app for Codex. Load one of your Codex threads, see the context that the
model sees, edit it, and fork it into a new thread in the same project. You can remove items,
edit text, add messages, cut the context at any point, go back to a context window from before a
compaction, and choose the model and reasoning effort for the fork.

The source thread does not change. The fork is a normal Codex thread: open it in the Codex app or
with `codex resume <id>` and send your next message.

## Requirements

- Python 3.9 or later. There are no Python packages to install.
- The Codex CLI (`codex`) on your `PATH`, or the Codex desktop app on macOS. Set `CODEX_BIN` to
  use a different binary.
- Threads in `~/.codex` (or in `$CODEX_HOME` if you set it).

Tested on macOS and Linux with Codex CLI 0.155 to 0.160.

## Start

    git clone https://github.com/andrewting19/codex-context-editor
    cd codex-context-editor
    ./run.sh                            # opens http://127.0.0.1:7317
    ./run.sh codex://threads/<id>       # opens with that thread loaded
    ./run.sh --port 8000 --no-browser   # other options

On Windows, use `python server.py`.

### On a remote machine

Start the server on the remote machine, then use an SSH tunnel:

    ssh -L 7317:127.0.0.1:7317 my-server
    python3 codex-context-editor/server.py --no-browser

Then open http://127.0.0.1:7317 on your computer. The fork is written on the remote machine.

## Use

1. Paste a thread link, such as `codex://threads/01a10d09-...`, or a thread id, and press **Load**.
2. Edit the context:
   - **Context window**: if the thread was compacted, select an earlier window. Window N is the
     full context just before compaction N.
   - **Fork here**: cut the context below this item.
   - **Remove / Restore**: remove an item. A tool call and its output are removed together.
   - **Edit**: change the text of a message, tool call, or tool output. **JSON** edits the raw item.
   - **+ User / + Assistant / + Dev**: add a new message below an item.
   - Use the filter buttons and the search box to find items.
3. Select the model and reasoning effort, set a name, and press **Create forked thread**.

## How it works

- Codex keeps each thread as a "rollout" file (JSON lines) in `~/.codex/sessions`. The editor
  reads the rollout, follows links to parent rollouts, and replays compactions and rollbacks to
  get the model context.
- A fork is a new rollout file with the edited items, plus the user and assistant events that
  the Codex app shows as a transcript. It has the same working folder as the source, so it shows
  in the same project.
- The editor starts a private `codex app-server` process to get the model list and to make Codex
  index the new thread.
- The server listens on 127.0.0.1 only and accepts requests only for local host names.

## Limits

- This tool uses the Codex rollout format, which is not a public API. A Codex update can break it.
- Reasoning items and compaction summaries are encrypted. You can remove them, but you cannot
  edit them. **Remove reasoning items** is on by default, because encrypted reasoning from one
  model can fail on another model.
- In the Codex app, the fork transcript shows user and assistant messages. Tool calls stay in the
  model context, but they do not show in the transcript.
- Codex can still be writing to a thread that is running. Fork from a thread that is idle.

## Test

`selftest.py` runs a full test against a running server. It makes a fork with an edit, runs one
real model turn on the fork (this uses your Codex quota), checks the reply, and archives the fork.

    ./run.sh --no-browser &
    python3 selftest.py                 # or: python3 selftest.py --thread <id>

## License

MIT

