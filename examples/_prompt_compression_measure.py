"""Measure token savings from prompt compression (PR1-2 token opt #2)."""
import sys
sys.path.insert(0, 'src')
from uams.utils.tokens import TokenEstimator

est = TokenEstimator()

old_episodic = "You are a memory consolidation assistant. Given a chronological list of agent events from one session, produce a single concise narrative (<= 200 words) capturing the user's goals, decisions, and outcomes. Preserve concrete facts (names, dates, numbers, preferences). Output ONLY the narrative text, no preamble."

new_episodic = "Summarize events into a <=200-word narrative. Preserve names, dates, numbers, preferences. Output only the narrative."

old_semantic = 'You are a fact extractor. Given a session narrative, extract atomic facts about the user (preferences, traits, biographical data). Return a JSON array of objects: [{"key": <short_key>, "value": <string>}]. Skip transient or session-specific info. Output ONLY the JSON array.'

new_semantic = 'Extract atomic user facts as JSON array: [{"key": str, "value": str}]. Skip session-specific info. Output only JSON.'

old_proc = 'You are a workflow analyzer. Given multiple session summaries, identify recurring workflows or interaction patterns. Return a JSON array: [{"pattern": <short_name>, "description": <one sentence>, "frequency": <int>}]. Only include patterns observed in >= 2 sessions. Output ONLY the JSON array.'

new_proc = 'Find recurring workflows across sessions. Return JSON array: [{"pattern": str, "description": str, "frequency": int}]. Only patterns seen >=2 times. Output only JSON.'

print('System prompt tokens (heuristic):')
e_old, e_new = est.estimate(old_episodic), est.estimate(new_episodic)
s_old, s_new = est.estimate(old_semantic), est.estimate(new_semantic)
p_old, p_new = est.estimate(old_proc), est.estimate(new_proc)
print(f'  Episodic  : {e_old:>3} -> {e_new:>3}  (save {e_old - e_new}, {100*(e_old-e_new)/e_old:.0f}%)')
print(f'  Semantic  : {s_old:>3} -> {s_new:>3}  (save {s_old - s_new}, {100*(s_old-s_new)/s_old:.0f}%)')
print(f'  Procedural: {p_old:>3} -> {p_new:>3}  (save {p_old - p_new}, {100*(p_old-p_new)/p_old:.0f}%)')

print()
old_total = e_old + s_old + p_old
new_total = e_new + s_new + p_new
print(f'Per-session summary (all 3 stages):')
print(f'  Old: {old_total} tokens -> New: {new_total} tokens')
print(f'  Save: {old_total - new_total} tokens ({100*(old_total-new_total)/old_total:.0f}%)')

print()
print('Event format (per event):')
old_evt = '[1700000000|USER_INPUT] I am vegetarian'
new_evt = '[USER_INPUT] I am vegetarian'
print(f'  Old: {est.estimate(old_evt)} -> New: {est.estimate(new_evt)} (save {est.estimate(old_evt) - est.estimate(new_evt)} per event)')
print(f'  20-event session: save {(est.estimate(old_evt) - est.estimate(new_evt)) * 20} tokens')

print()
print('Plus: shorter system prompts = HIGHER prompt-cache hit rate on OpenAI/Anthropic')
print('(cached prefix reuse typically saves 50-90% input token $)')