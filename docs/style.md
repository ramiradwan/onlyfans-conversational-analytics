# Documentation style

Use this guide for repository prose: READMEs, guides, ADR summaries, contributor docs, and release notes.

## Core rules

- Give each document one job. Split it when it starts serving more than one or two distinct tasks or concepts.
- Lead with the answer, action, or constraint. Put background after it.
- Keep one idea per paragraph. Most paragraphs should be one to three sentences.
- Prefer short sentences. Aim for fewer than about 25 words when the meaning stays clear.
- Use plain, precise words and active voice.
- Delete words that do not change the meaning.
- Use one term for one concept. Keep **Agent**, **Brain**, **Bridge**, **launcher**, and **provisioning** consistent.
- Keep detailed facts in one canonical place. Link to them instead of copying them into several documents.
- If a page is secondary to an authoritative source, summarize and link to that source instead of restating its full rules or tables.
- Split large contracts by independent concern. Keep a stable index page when existing links should continue to work.
- Do not hard-wrap prose. Let the renderer wrap it.

## Structure

- Use sentence-case headings that tell the reader what the section contains or helps them do.
- Use numbered lists for procedures. Give each step one main action.
- Use bullets for independent items, not for paragraphs disguised as a list.
- Keep notes rare. Put prerequisites and required warnings in the main flow before the relevant step.
- Make commands copyable. State the required platform or working directory when it is not obvious.
- Prefer a short link to the canonical detail over a long aside.

## Verify implementation-dependent docs

Put a `<!-- CODE-VERIFY: ... -->` comment near the top of a document when it contains implementation-specific facts such as routes, file names, configuration names, permissions, storage behavior, limits, process behavior, or commands.

Before editing those facts, verify them against source code, tests, configuration, or workflows. Another prose document is not sufficient evidence.

Keep the comment specific enough to identify what must be checked. The comment is intentionally hidden in rendered Markdown.

## Tone

Write neutral technical documentation for a reader who understands ordinary software concepts but may not know this repository.

Explain specialized terms when they are necessary. Prefer familiar words over academic, architectural, or research jargon when both are accurate. Do not make the reader decode terminology that is not needed to use or understand the system.

Describe what the software does, what the reader must do, and what constraints apply. Do not sell the product or praise the implementation.

Avoid hype and marketing language. Do not use claims such as *robust*, *seamless*, *powerful*, *comprehensive*, *cutting-edge*, *best-in-class*, *crucial*, or *revolutionary* unless the document defines a precise, verifiable meaning that requires the term.

Do not call ordinary behavior *impressive*, *advanced*, *sophisticated*, or *enterprise-grade*. State the observable behavior instead.

Do not call a task *simple*, *easy*, *obvious*, or *quick*. State what the reader must do.

## AI-assisted drafts

Treat generated text as a draft, never as a source of truth. Verify every claim, path, command, version, constant, and link before merging it.

Remove common unedited-model habits:

- canned framing such as "It's important to note", "In summary", "Overall", "This section explores", and "There are several key";
- repeated transitions such as "Additionally", "Moreover", and "Furthermore";
- formulaic rhetoric such as "not just X, but Y" or repeated three-part constructions;
- restating the same point in an introduction, body, and conclusion;
- generic benefits that are not tied to observable behavior;
- excessive bold text, em dashes, parenthetical asides, blockquotes, or headings used only to create rhythm;
- inflated language that makes ordinary implementation details sound novel, strategic, or exceptional;
- unnecessary jargon or abstract nouns when a direct verb or concrete term is clearer.

Do not ban a normal word or punctuation mark just because models often use it. The problem is repetitive, low-information writing. Rewrite the pattern, not the vocabulary.

Stop when the reader has enough information to act or understand the topic. A shorter complete document is better than a padded one.

## Before merge

Read the changed prose once without editing. Then remove repetition, filler, stale facts, hype, unnecessary jargon, and any sentence that exists only to sound polished. Check that links and commands still work.
