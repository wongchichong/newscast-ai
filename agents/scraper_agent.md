# ScraperAgent

## Role
Fetch and process a news article URL. Extract text, images, and build a scroll video.

## Steps
1. Call `scrape_article` with the target URL
2. Verify:
   - `text` is not empty (if empty, report failure)
   - `scroll_video` was created (if not, note it — composer will handle gracefully)
3. Return the full scrape result JSON including `job_id`

## Output
Return JSON with: title, text (first 500 chars), images count, scroll_video path, job_id
