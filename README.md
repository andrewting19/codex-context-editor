# Codex Context Editor

A local web page to load a Codex thread, edit its model context, and fork it as a new thread in the same project.

## Start

    ./run.sh                      # opens http://127.0.0.1:7317
    ./run.sh <thread-id>          # opens with that thread loaded

## Use

1. Paste a link such as `codex://threads/01a10d09-...` and press Load.
2. Edit the context:
   - If the thread was compacted, use **Context window** above the list to select an earlier
     window. Window N is the full context just before compaction N. Then use **Fork here**
     to start the fork at any point in that window.
   - **Remove / Restore** an item. A tool call and its output are removed together.
   - **Edit** the text of a message, tool call, or tool output. **JSON** edits the raw item.
   - **+ User / + Assistant / + Dev** adds a new message below an item.
   - **Fork here** drops everything below that item.
3. Select the model and reasoning effort, set a name, and press **Create forked thread**.
   The fork opens in Codex. Send your next message there to continue.

## How it works

- The page shows the context that the model sees. It follows the rollout files in
  `~/.codex/sessions` through parent threads and applies compactions.
- A fork is a new rollout file with the edited items, plus user and assistant events so the
  Codex app shows a transcript. The fork has the same working folder as the source, so it shows
  in the same project. The source thread does not change.
- A private `codex app-server` (from the ChatGPT app) gives the model list and registers the fork.

## Limits

- Reasoning and compaction items are encrypted. You can remove them, but you cannot edit them.
  "Remove reasoning items" is on by default because encrypted reasoning can fail on another model.
- The fork transcript in Codex shows user and assistant messages. Tool calls stay in the model
  context but do not show in the transcript.
