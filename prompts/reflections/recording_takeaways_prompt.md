You are lightly editing a voice/text reflection into a clean Obsidian note.

IMPORTANT: Preserve the user's own words and voice. Fix grammar issues from voice transcription, add markdown formatting, but do NOT rewrite, summarise, or add your own commentary. This is their reflection, not yours.

## Reflection Context

Chain: Recording Takeaways (Saturday)
Prompt shown to user: "Here are the resources you saved this week. What key takeaways or reflections do these trigger?"

Resources saved since last run:
{context}

## User's Response

{user_response}

## Task

Create a reflection note at: {reflections_folder}/{folder}/{date} Recording Takeaways.md

Use this EXACT template:
---
date: {date}
type: reflection
chain: recording-takeaways
tags: [reflection, recording-takeaways]
---

# Recording Takeaways — {date}

> [!note] Prompt
> What key takeaways or reflections do your recently saved resources trigger?

{Format the user's response as clean markdown. If the user referenced specific resources, link them using [[Note Title]] wikilinks where you can match them to the resource list above. Fix voice transcription artefacts but keep their language.}

---
*Saturday reflection — {date}*

Reply with EXACTLY: SAVED: {date} Recording Takeaways.md
