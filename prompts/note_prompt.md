You are saving a resource to an Obsidian vault for personal knowledge management.
Your working directory is the vault root. Use your file tools to read existing notes.

---
RESOURCE CONTENT:
{content_or_fetch_failed_message}

WHY SAVED (voice note / context):
{voice_context_or_none}

SOURCE: {source_url_or_type}
SOURCE_ID: {source_id}
DATE: {date}
{image_line_if_present}
---

TASK:
1. Search the vault for the 3–5 most relevant existing notes (read files, check headings/tags).
2. Write a concise summary (3–8 sentences) emphasising what is useful given the stated reason for saving.
3. Generate tags dynamically from the content (e.g. [resource, machine-learning, python]).
4. Create the note at: {resource_folder}/{date} {sanitised_title}.md

Title sanitisation rules:
- No slashes, colons, pipes, question marks, or asterisks
- Maximum 60 characters
- Title case

Use this EXACT template (do not add sections or deviate from frontmatter keys):
---
source: {source_url_or_type}
source_id: {source_id}
date: {date}
type: resource
tags: [resource, <dynamic tags>]
---

# {Title}

> [!note] Why I saved this
> {voice_context_or_none_text}

## Summary

{summary}

## Related Notes

- [[Note Title]] — one sentence on relevance
(one line per related note; omit section if none found)

---
*Saved via Telegram · {datetime}*

5. Reply with EXACTLY this line and nothing else after writing the file:
SAVED: <filename_without_path>
