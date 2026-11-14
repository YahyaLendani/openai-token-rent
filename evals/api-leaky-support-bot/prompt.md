---
tags: [openai-token-rent]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

our openai bill for the support bot basically tripled since we moved it to gpt-6.1-sol and i cant figure out why. its in support_bot.py. we get roughly 600 requests an hour, ~400 users active at once, the kb dict is about 28k tokens and changes maybe twice a day. can you fix the code so its cheaper without making answers worse, and tell me roughly how much we save

Input files: support_bot.py
