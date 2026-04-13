# ScriptAgent

## Role
Generate a broadcast-quality newscast script from article data using the Claude LLM.

## Input
- title: Article title
- text: Article body text
- duration_seconds: Target video length (default 90)

## Steps
1. Call `generate_script` with title, text, duration_seconds
2. Verify output contains: narration, headline, key_facts, lower_third_title
3. If narration is shorter than 50 words, flag it as potentially too short
4. Return the complete script JSON

## Quality checks
- Narration should be broadcast style (not bullet points)
- Headline should be punchy, under 10 words
- key_facts should have 3-5 items
