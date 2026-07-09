"""Generates a brutal, Russian-language "roast" of one chat participant based on their
own messages, via an OpenAI chat model. Triggered by the "прожарь меня" listener command.
"""

from openai import OpenAI, OpenAIError

from errors import ChatSummaryError
from summarizer import chunk_transcript

ROAST_SYSTEM_PROMPT = """\
Ты -- беспощадный комик, который жарит (roast) участника чата на основе ЕГО СОБСТВЕННЫХ \
сообщений за последнее время. Это шуточный ритуал взаимных подколов между своими, не \
настоящее оскорбление.

Правила:
- Пиши ИСКЛЮЧИТЕЛЬНО на русском языке.
- Будь максимально едким, дерзким и жёстким -- не смягчай и не извиняйся. Свободно используй \
мат и грубые выражения там, где это усиливает шутку.
- Найди РОВНО 5 главных поводов для прожарки, основываясь ТОЛЬКО на том, что реально есть в \
сообщениях (повторяющиеся фразочки, странные привычки, нелепые заявления, противоречия, \
капслок, вечные темы, забавные факапы и т.п.). Не выдумывай факты, которых нет в транскрипте.
- НЕ жарь за орфографические, грамматические или пунктуационные ошибки, опечатки и т.п. -- \
это не смешно и не по адресу, жарь за суть того, что человек пишет, а не за то, как он это \
пишет.
- Каждый пункт: короткий дерзкий **жирный** заголовок, затем 1-2 едких предложения, \
объясняющих почему это смешно/жалко/нелепо, с конкретной отсылкой к тому, что человек \
реально писал.
- В конце добавь одну короткую убийственную панчлайн-строку, подводящую итог.
- Не переходи на реальные угрозы, разжигание ненависти по защищённым признакам (раса, \
национальность, религия, ориентация и т.п.) или разглашение приватной информации, которой не \
было в сообщениях -- жарь за то, что человек сам написал, а не за то, кто он.
- Формат: 5 пронумерованных пунктов и финальная панчлайн-строка, ничего больше -- без \
предисловий и без заключительных смягчений в духе "но на самом деле ты классный".
"""

MAP_PROMPT = """\
Ниже -- ЧАСТЬ {part} из {total} сообщений участника {target_name} (формат \
"[YYYY-MM-DD HH:MM] Имя: текст"). Выпиши из этой части всё, что годится для прожарки: \
повторяющиеся словечки/фразы, странные привычки, нелепые заявления, противоречия, забавные \
факапы, типичные темы, капслок -- что угодно характерное. НЕ фиксируй орфографические, \
грамматические или пунктуационные ошибки и опечатки -- это не годится для прожарки. Зафиксируй \
это как короткие сырые заметки для объединения с другими частями. Шутки и панчлайны пока НЕ \
пиши, только сырой материал.

Сообщения:
{transcript}
"""

FINAL_FROM_NOTES_PROMPT = """\
Участник, которого нужно прожарить: {target_name}

Сообщений оказалось слишком много, поэтому их разбили на {total} частей и по каждой сделали \
черновые заметки. Заметки по всем частям:
{notes}

Используй эти заметки, чтобы написать финальную прожарку по правилам из системного промпта.
"""

FINAL_FROM_TRANSCRIPT_PROMPT = """\
Участник, которого нужно прожарить: {target_name}

Его сообщения (формат "[YYYY-MM-DD HH:MM] Имя: текст"):
{transcript}
"""


def _chat(client: OpenAI, model: str, system_prompt: str, user_prompt: str) -> str:
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.9,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except OpenAIError as e:
        raise ChatSummaryError(f"OpenAI API call failed while generating a roast: {e}") from e

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ChatSummaryError("OpenAI returned an empty roast -- try again or use a different model.")
    return content.strip()


def roast_person(
    api_key: str,
    model: str,
    target_name: str,
    lines: list[str],
    max_chunk_tokens: int = 6000,
) -> str:
    """Returns a 5-point Russian-language roast of `target_name`, generated from `lines`
    (their own chat messages, formatted like `telegram_fetch.format_transcript_lines`).
    Long histories are map-reduced into notes first, same approach as `summarize_transcript`."""
    if not api_key or not api_key.strip():
        raise ChatSummaryError("OpenAI API key is missing.")
    if not model or not model.strip():
        raise ChatSummaryError("OpenAI model name is missing.")
    if not lines:
        raise ChatSummaryError("No messages to roast.")

    client = OpenAI(api_key=api_key)
    chunks = chunk_transcript(lines, max_chunk_tokens, model)

    if len(chunks) == 1:
        prompt = FINAL_FROM_TRANSCRIPT_PROMPT.format(target_name=target_name, transcript=chunks[0])
        return _chat(client, model, ROAST_SYSTEM_PROMPT, prompt)

    notes_parts = []
    for i, chunk in enumerate(chunks, start=1):
        prompt = MAP_PROMPT.format(part=i, total=len(chunks), target_name=target_name, transcript=chunk)
        notes = _chat(client, model, ROAST_SYSTEM_PROMPT, prompt)
        notes_parts.append(f"--- Часть {i} ---\n{notes}")

    combined_notes = "\n\n".join(notes_parts)
    final_prompt = FINAL_FROM_NOTES_PROMPT.format(
        target_name=target_name, total=len(chunks), notes=combined_notes
    )
    return _chat(client, model, ROAST_SYSTEM_PROMPT, final_prompt)
