# flows_contact.py - System kontaktu z właścicielem
"""
Obsługuje:
- Prośby o kontakt z właścicielem
- Przekierowanie rozmowy (transfer)
- Zbieranie i wysyłanie wiadomości

Działa dla OBU trybów:
- Rezerwacje włączone
- Tylko informacyjnie
"""

from pipecat_flows import FlowManager, FlowsFunctionSchema
from loguru import logger
import asyncio
from flows_helpers import _assistant_gender

# ============================================================================
# HELPER: Streszczenie rozmowy
# ============================================================================
async def generate_conversation_summary(flow_manager) -> str:
    """Generuje 2-zdaniowe streszczenie rozmowy przez GPT"""
    try:
        import openai
        import os
        
        # Zbierz kontekst rozmowy
        context = flow_manager.get_current_context()
        
        # Wyciągnij tylko user/assistant messages
        conversation = []
        for msg in context:
            if msg.get("role") in ["user", "assistant"]:
                content = msg.get("content", "")
                if content and len(content) > 2:
                    role = "Klient" if msg["role"] == "user" else "Bot"
                    conversation.append(f"{role}: {content}")
        
        if not conversation:
            return "Brak treści rozmowy."
        
        conversation_text = "\n".join(conversation[-10:])  # Ostatnie 10 wiadomości
        
        # Szybkie streszczenie przez GPT
        client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "system",
                "content": "Streść poniższą rozmowę w MAKSYMALNIE 2 zdaniach po polsku. Skup się na tym czego klient chciał i jakie informacje otrzymał."
            }, {
                "role": "user", 
                "content": conversation_text
            }],
            max_tokens=100,
            temperature=0.3
        )
        
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"Summary generation error: {e}")
        return "Nie udało się wygenerować streszczenia."
# ============================================================================
# GŁÓWNA FUNKCJA - GPT wywołuje gdy klient chce kontakt
# ============================================================================

def contact_owner_function(tenant: dict) -> FlowsFunctionSchema:
    """Klient chce kontakt z właścicielem (wiadomość LUB połączenie)"""
    return FlowsFunctionSchema(
        name="contact_owner",
        description="""Klient chce kontakt z właścicielem. Użyj gdy:
- "chcę porozmawiać z właścicielem"
- "proszę o kontakt"  
- "czy mogę zostawić wiadomość"
- "połącz mnie"
- "przekieruj mnie"
- "chcę rozmawiać z człowiekiem"
- klient jest sfrustrowany i potrzebuje pomocy człowieka
- nie możesz pomóc i klient potrzebuje właściciela
- klient chce się umówić ale rezerwacje są wyłączone (zaproponuj wiadomość)
- klient chce rozmawiać z konkretnym pracownikiem/fryzjerem/fryzjerką""",
        properties={
            "reason": {
                "type": "string",
                "description": "Krótko: dlaczego klient chce kontakt"
            },
            "customer_name": {
                "type": "string",
                "description": "Imię klienta jeśli już podał"
            },
            "message": {
                "type": "string",
                "description": "Treść wiadomości jeśli klient już powiedział co przekazać"
            }
        },
        required=["reason"],
        handler=lambda args, fm: handle_contact_owner(args, fm, tenant),
    )


async def handle_contact_owner(args: dict, flow_manager: FlowManager, tenant: dict):
    """Obsługa kontaktu z właścicielem — zapis wiadomości lub transfer"""
    reason = args.get("reason", "").lower()
    customer_name = args.get("customer_name", "")
    message = args.get("message", "")
    
    logger.info(f"📞 Contact owner: reason='{reason}', name='{customer_name}', msg='{message[:30] if message else ''}'")
    
    # Zapisz imię jeśli podane
    if customer_name:
        flow_manager.state["contact_name"] = customer_name
    
    # Jeśli mamy imię (z tego lub wcześniejszego kroku) + wiadomość → zapisz od razu
    existing_name = flow_manager.state.get("contact_name")
    if existing_name and message and len(message) >= 5:
        meta_starts = ["klient chce", "klient prosi", "klient jest", "klient potrzebuje"]
        is_meta = any(message.lower().startswith(m) for m in meta_starts)
        if not is_meta:
            logger.info(f"📞 Have name + valid message — saving directly")
            flow_manager.state["contact_message"] = message
            return await save_and_confirm_message(flow_manager, tenant, existing_name, message)
        else:
            logger.info(f"📞 Rejecting GPT meta-description: '{message[:50]}'")
    
    # Sprawdź czy transfer dostępny
    transfer_enabled = tenant.get("transfer_enabled", 0) == 1
    transfer_number = tenant.get("transfer_number", "")
    has_transfer = transfer_enabled and transfer_number
    
    logger.info(f"📞 Transfer available: {has_transfer} (enabled={transfer_enabled}, number='{transfer_number}')")
    
    # NAJPIERW sprawdź czy klient chce WIADOMOŚĆ
    message_keywords = ["wiadomość", "zostawić", "przekazać", "niech oddzwoni", "napisać"]
    wants_message = any(kw in reason for kw in message_keywords)
    
    if wants_message:
        logger.info(f"📞 MESSAGE requested based on reason keywords")
        return (None, create_collect_message_content_node(tenant, "Dobrze, co mam przekazać właścicielowi?"))

    # POTEM sprawdź czy chce TRANSFER
    transfer_keywords = ["połącz", "przekieruj", "bezpośrednio", "człowiek", "rozmawiać z"]
    wants_transfer = any(kw in reason for kw in transfer_keywords)

    if wants_transfer:
        if has_transfer:
            logger.info(f"📞 AUTO-TRANSFER based on reason keywords")
            return await execute_transfer(flow_manager, tenant)
        else:
            logger.info(f"📞 Transfer requested but DISABLED - offering message")
            return (None, create_collect_message_content_node(
                tenant,
                "Bezpośredniego połączenia niestety nie mam, ale mogę przekazać wiadomość właścicielowi. Co mam przekazać?"
            ))
    elif has_transfer:
        return (None, create_contact_choice_node(tenant))
    else:
        return (None, create_collect_message_content_node(tenant, "Chętnie przekażę wiadomość. Co mam przekazać właścicielowi?"))
# ============================================================================
# NODE: Wybór - wiadomość czy połączenie (gdy transfer dostępny)
# ============================================================================

def create_contact_choice_node(tenant: dict) -> dict:
    """Pytanie o wybór - DWIE osobne funkcje (prostsze dla GPT)"""
    return {
        "name": "contact_choice",
        "pre_actions": [
            {"type": "tts_say", "text": "Mogę przekazać wiadomość do właściciela lub połączyć bezpośrednio. Wiadomość czy bezpośrednie połączenie?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": "Klient wybiera sposób kontaktu z właścicielem."
        }],
        "task_messages": [{
            "role": "system",
            "content": """Klient odpowiada na pytanie: wiadomość czy połączenie.
Wywołaj JEDNĄ z funkcji:
- do_transfer → gdy klient chce POŁĄCZENIE
- do_message → gdy klient chce WIADOMOŚĆ
- start_booking → gdy klient zmienił zdanie i WYRAŹNIE chce się umówić przez bota
- end_conversation → gdy klient się ŻEGNA

Na inne pytania (cennik, godziny) → odpowiedz krótko tekstem i ponów pytanie o wybór."""
        }],
        "functions": [
            do_transfer_function(tenant),
            do_message_function(tenant),
            _get_start_booking_function(),
            _get_end_conversation_function(),
        ]
    }


def do_transfer_function(tenant: dict) -> FlowsFunctionSchema:
    """Prosta funkcja bez parametrów - łatwiejsza dla GPT"""
    return FlowsFunctionSchema(
        name="do_transfer",
        description="Klient chce POŁĄCZENIE telefoniczne z właścicielem. Użyj gdy mówi: połączyć, bezpośrednio, tak, proszę, teraz.",
        properties={},
        required=[],
        handler=lambda args, fm: handle_do_transfer(args, fm, tenant),
    )


def do_message_function(tenant: dict) -> FlowsFunctionSchema:
    """Prosta funkcja bez parametrów - łatwiejsza dla GPT"""
    return FlowsFunctionSchema(
        name="do_message",
        description="Klient chce zostawić WIADOMOŚĆ. Użyj gdy mówi: wiadomość, zostawić, niech oddzwoni, przekaż, nie trzeba łączyć.",
        properties={},
        required=[],
        handler=lambda args, fm: handle_do_message(args, fm, tenant),
    )


async def handle_do_transfer(args: dict, flow_manager: FlowManager, tenant: dict):
    """Transfer - wykonaj natychmiast"""
    logger.info("📞 DO_TRANSFER called - executing transfer")
    return await execute_transfer(flow_manager, tenant)


async def handle_do_message(args: dict, flow_manager: FlowManager, tenant: dict):
    """Wiadomość - zbierz dane"""
    logger.info("📝 DO_MESSAGE called - collecting data")
    return (None, create_collect_message_content_node(tenant))


# ============================================================================
# NODE: Zbieranie imienia
# ============================================================================

def create_collect_contact_name_node(tenant: dict, intro_text: str = None) -> dict:
    """Zbierz imię klienta"""
    tts_text = intro_text if intro_text else "Na jakie imię zapisuję?"
    return {
        "name": "collect_contact_name",
        "pre_actions": [
            {"type": "tts_say", "text": tts_text}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": "Zbierasz imię klienta do wiadomości dla właściciela."
        }],
        "task_messages": [{
            "role": "system",
            "content": """Klient poda imię lub nazwisko.

ZASADA PRIORYTETOWA: Jedno słowo lub krótka odpowiedź = ZAWSZE imię. Nigdy pożegnanie.
Gdy klient poda imię/nazwisko → wywołaj WYŁĄCZNIE set_contact_name. Nic więcej.
ZAKAZ wywoływania end_conversation jednocześnie z set_contact_name lub zaraz po — to dwie osobne, wzajemnie wykluczające się akcje.
Tylko gdy klient WPROST rezygnuje słowami "nie chcę", "nieważne", "do widzenia", "żegnaj" → wywołaj end_conversation.
NIE powtarzaj imienia klienta w odpowiedzi — po prostu wywołaj set_contact_name.

WYJĄTKI — NIE traktuj jako imię:
- "halo", "słucham", "cześć", "jest tam ktoś" → klient sprawdza połączenie → odpowiedz "Słyszę Pana, na jakie imię przekazać wiadomość?" i czekaj dalej
- jeśli klient ponownie prosi o rozmowę z pracownikiem/właścicielem → powiedz "Rozumiem, niestety połączyć nie mogę — mogę przekazać wiadomość. Na jakie imię?" i czekaj dalej
- NIE używaj zasad z głównego systemu ("Tym się nie zajmuję") — jesteś tylko w trybie zbierania imienia"""
        }],
        "functions": [
            set_contact_name_function(tenant),
            _get_start_booking_function(),
            _get_end_conversation_function(),
        ]
    }


def set_contact_name_function(tenant: dict) -> FlowsFunctionSchema:
    """Zapisz imię"""
    return FlowsFunctionSchema(
        name="set_contact_name",
        description="Klient podał imię/nazwisko",
        properties={
            "name": {"type": "string", "description": "Imię lub nazwisko klienta"}
        },
        required=["name"],
        handler=lambda args, fm: handle_set_contact_name(args, fm, tenant),
    )


async def handle_set_contact_name(args: dict, flow_manager: FlowManager, tenant: dict):
    """Zapisz imię i przejdź do treści"""
    name = args.get("name", "").strip()
    
    # Walidacja
    invalid_names = ["pan", "pani", "tak", "nie", "halo", "słucham", "proszę"]
    if not name or len(name) < 2 or name.lower() in invalid_names:
        nd = _assistant_gender(tenant.get("assistant_name", "Ania"))["nie_dosłyszałam"]
        return ({"status": "error", "message": f"{nd}. Jak mogę zapisać?"},
                create_collect_message_content_node(tenant))
    
    # Usuń "pan/pani" z początku
    for prefix in ["pan ", "pani "]:
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    
    name = name.strip().title()
    flow_manager.state["contact_name"] = name
    logger.info(f"📝 Contact name: {name}")
    
    # Jeśli już mamy wiadomość - zapisz
    if flow_manager.state.get("contact_message"):
        return await save_and_confirm_message(
            flow_manager, tenant, name, flow_manager.state["contact_message"]
        )
    
    # Przejdź do treści BEZ powtarzania imienia (TTS źle wymawia polskie imiona)
    return (None, create_collect_message_content_node(tenant))


# ============================================================================
# NODE: Zbieranie treści wiadomości
# ============================================================================

def create_collect_message_content_node(tenant: dict, intro_text: str = None) -> dict:
    tts_text = intro_text if intro_text else "Co mam przekazać właścicielowi?"
    return {
        "name": "collect_message_content",
        "pre_actions": [
            {"type": "tts_say", "text": tts_text}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": "Zbierasz treść wiadomości dla właściciela."
        }],
        "task_messages": [{
            "role": "system",
            "content": """Klient poda treść wiadomości dla właściciela.

Gdy klient podaje treść wiadomości → wywołaj set_contact_message z dokładnie tym co powiedział.
Nawet jeśli wiadomość jest krótka lub zawiera wulgaryzmy — zapisz ją dosłownie.

Gdy klient ODMAWIA zostawienia wiadomości (mówi: "nie chcę", "nie trzeba", "nieważne", "nie, dziękuję", "rezygnuję") → wywołaj end_conversation. NIE zapisuj odmowy jako wiadomości.
Gdy klient się ŻEGNA ("do widzenia", "pa", "żegnaj") → wywołaj end_conversation.

NIE odpowiadaj tekstem — TYLKO wywołaj jedną z funkcji.
ZAKAZ wywoływania end_conversation jednocześnie z set_contact_message — to wzajemnie wykluczające się akcje."""
        }],
        "functions": [
            set_contact_message_function(tenant),
            _get_start_booking_function(),
            _get_end_conversation_function(),
        ]
    }

def set_contact_message_function(tenant: dict) -> FlowsFunctionSchema:
    """Zapisz treść wiadomości"""
    return FlowsFunctionSchema(
        name="set_contact_message",
        description="Klient podał treść wiadomości",
        properties={
            "message": {"type": "string", "description": "Treść wiadomości"}
        },
        required=["message"],
        handler=lambda args, fm: handle_set_contact_message(args, fm, tenant),
    )


async def handle_set_contact_message(args: dict, flow_manager: FlowManager, tenant: dict):
    """Zapisz wiadomość i wyślij"""
    message = args.get("message", "").strip()

    if not message or len(message) < 3:
        nd = _assistant_gender(tenant.get("assistant_name", "Ania"))["nie_dosłyszałam"]
        return ({"status": "error", "message": f"{nd}. Co mam przekazać?"},
                create_collect_message_content_node(tenant))

    # Ochrona przed zapisem odmowy lub meta-opisu LLM zamiast prawdziwej wiadomości
    msg_lower = message.lower()
    refusal_tokens = ["nie chcę", "nie chce", "nie trzeba", "nieważne", "rezygnuję",
                      "do widzenia", "żegnaj", "pa pa", "nie, dziękuję", "nie dziękuję"]
    meta_tokens = ["klient podziękował", "klient nie chce", "klient rezygnuje",
                   "klient się żegna", "klient nie chciał", "klient zmienił",
                   "klient nie zostawił", "klient powiedział dziękuję"]
    is_refusal = any(t in msg_lower for t in refusal_tokens)
    is_meta    = any(t in msg_lower for t in meta_tokens)
    if is_refusal or is_meta:
        logger.info(f"📝 Odmowa/meta w set_contact_message — nie zapisuję: '{message[:60]}'")
        from flows import create_end_node
        return (None, create_end_node(message_saved=False))

    caller_phone = flow_manager.state.get("caller_phone", "")
    name = flow_manager.state.get("contact_name") or caller_phone or "Nieznany"

    return await save_and_confirm_message(flow_manager, tenant, name, message)


async def save_and_confirm_message(flow_manager: FlowManager, tenant: dict, name: str, message: str):
    """Zapisz wiadomość, wyślij email ze streszczeniem W TLE, potwierdź klientowi I ROZŁĄCZ"""
    from flows import create_end_node, send_message_email
    
    caller_phone = flow_manager.state.get("caller_phone", "nieznany")
    
    logger.info(f"📧 Saving message: {name} - {message[:50]}...")
    
    owner_email = tenant.get("notification_email") or tenant.get("email")
    
    # 🔥 NOWE: Wyślij email W TLE - nie blokuj odpowiedzi!
    async def send_email_with_summary():
        if owner_email:
            try:
                conversation_summary = await generate_conversation_summary(flow_manager)
                logger.info(f"📋 Summary generated: {conversation_summary[:50]}...")
                
                message_with_summary = f"{message}\n\n---\n📋 Streszczenie rozmowy:\n{conversation_summary}"
                await send_message_email(tenant, name, message_with_summary, caller_phone, owner_email)
                logger.info(f"📧 Email sent to: {owner_email}")
            except Exception as e:
                logger.error(f"📧 Email error: {e}")
        else:
            logger.warning("📧 No owner email configured!")
    
    # Uruchom w tle - NIE CZEKAJ na email!
    asyncio.create_task(send_email_with_summary())
    
    flow_manager.state["contact_name"] = None
    flow_manager.state["contact_message"] = None
    flow_manager.state["conversation_ended"] = True
    
    # Zaplanuj rozłączenie po TTS
    async def auto_hangup_after_message():
        await asyncio.sleep(3.0)
        try:
            from pipecat.frames.frames import EndFrame
            await flow_manager.task.queue_frame(EndFrame())
            logger.info("🔚 EndFrame sent after message saved")
        except Exception as e:
            logger.error(f"Error sending EndFrame: {e}")
    
    asyncio.create_task(auto_hangup_after_message())
    
    return (f"Wiadomość przekazana. Właściciel oddzwoni najszybciej jak to możliwe.",
        create_end_node(message_saved=True))
# ============================================================================
async def execute_transfer(flow_manager: FlowManager, tenant: dict):
    """Wykonaj transfer rozmowy do właściciela"""
    from flows import create_end_node
    from helpers import db
    
    transfer_number = tenant.get("transfer_number", "")
    
    if not transfer_number:
        logger.error("📞 No transfer number configured!")
        return ("Przepraszam, przekierowanie chwilowo niedostępne. Czy mogę przekazać wiadomość?",
                create_collect_message_content_node(tenant))
    
    call_sid = flow_manager.state.get("call_sid")
    
    if not call_sid:
        logger.error("📞 No call_sid for transfer!")
        return ("Przepraszam, wystąpił problem. Czy mogę przekazać wiadomość?",
                create_collect_message_content_node(tenant))
    
    # Formatuj numer
    transfer_number = transfer_number.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    if transfer_number.startswith("0048"):
        transfer_number = transfer_number[4:]
    elif transfer_number.startswith("48") and len(transfer_number) == 11:
        transfer_number = transfer_number[2:]
    if not transfer_number.startswith("+"):
        transfer_number = f"+48{transfer_number}"
    
    if len(transfer_number) < 12:
        logger.error(f"📞 Invalid transfer number: {transfer_number}")
        return ("Przepraszam, numer przekierowania jest nieprawidłowy. Czy mogę przekazać wiadomość?",
                create_collect_message_content_node(tenant))
    
    logger.info(f"📞 Executing transfer: {call_sid} → {transfer_number}")
    
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transfer_requests (
                call_sid TEXT PRIMARY KEY,
                transfer_number TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        
        await db.execute(
            """INSERT OR REPLACE INTO transfer_requests (call_sid, transfer_number, status, created_at)
               VALUES (?, ?, 'pending', datetime('now'))""",
            [call_sid, transfer_number]
        )
        logger.info(f"📞 Transfer request saved")
        
    except Exception as e:
        logger.error(f"📞 Failed to save transfer: {e}")
        return ("Przepraszam, wystąpił problem z przekierowaniem. Czy mogę przekazać wiadomość?",
                create_collect_message_content_node(tenant))
    
    flow_manager.state["transfer_requested"] = True
    flow_manager.state["conversation_ended"] = True
    
    async def close_for_transfer():
        await asyncio.sleep(3.0)
        try:
            from pipecat.frames.frames import EndFrame
            await flow_manager.task.queue_frame(EndFrame())
            logger.info("🔚 EndFrame sent for transfer")
        except Exception as e:
            logger.error(f"Error sending EndFrame: {e}")
    
    asyncio.create_task(close_for_transfer())
    
    return (None, _create_transfer_end_node())


def _create_transfer_end_node() -> dict:
    """Node końcowy dla transferu"""
    return {
        "name": "transfer_end",
        "respond_immediately": False,
        "pre_actions": [
            {"type": "tts_say", "text": "Łączę z właścicielem, proszę chwilę poczekać."}
        ],
        "post_actions": [
            {"type": "end_conversation"}
        ],
        "role_messages": [],
        "task_messages": [],
        "functions": []
    }


def _get_end_conversation_function():
    """Lazy import end_conversation_function"""
    from flows import end_conversation_function
    return end_conversation_function()

def _get_start_booking_function():
    from flows_booking_simple import start_booking_function_simple
    return start_booking_function_simple()


# ============================================================================
# LEAD QUALIFICATION — zbieranie zgłoszeń serwisowych
# ============================================================================

def submit_lead_function(tenant: dict) -> FlowsFunctionSchema:
    """Klient opisuje problem wymagający kontaktu ze specjalistą"""
    return FlowsFunctionSchema(
        name="submit_lead",
        description="""Klient opisuje problem, usterkę, awarię lub sprawę wymagającą kontaktu ze specjalistą.
Użyj gdy klient: opisuje problem techniczny, awarię, reklamację, pyta o wycenę niestandardowej pracy.
NIE używaj gdy klient chce standardowej rezerwacji usługi z cennika.""",
        properties={
            "problem":  {"type": "string", "description": "Krótki opis problemu klienta (1-2 zdania)"},
            "details":  {"type": "string", "description": "Dodatkowe szczegóły: marka/model/adres/od kiedy itp."},
            "urgency":  {"type": "string", "enum": ["high", "normal"],
                         "description": "high = awaria/pilne/nie działa wcale, normal = zwykłe zgłoszenie"},
        },
        required=["problem"],
        handler=lambda args, fm: handle_submit_lead(args, fm, tenant),
    )


async def handle_submit_lead(args: dict, flow_manager: FlowManager, tenant: dict):
    problem = args.get("problem", "").strip()
    details = args.get("details", "").strip()
    urgency = args.get("urgency", "normal")
    caller_phone = flow_manager.state.get("caller_phone", "nieznany")
    logger.info(f"🔧 Lead: urgency={urgency}, problem={problem[:60]}")
    if urgency == "high":
        confirmation = "To wygląda na pilną sprawę. Przekażę zgłoszenie naszemu specjaliście — oddzwoni jeszcze dziś lub najszybciej jak to możliwe. Czy mogę pomóc w czymś jeszcze?"
    else:
        confirmation = "Dobrze. Już przekazuję zgłoszenie naszemu specjaliście, który oddzwoni najszybciej jak to możliwe. Czy mogę pomóc w czymś jeszcze?"
    return await _save_and_send_lead(flow_manager, tenant, caller_phone, problem, details, urgency, confirmation)


async def _save_and_send_lead(flow_manager, tenant, caller_phone, problem, details, urgency, confirmation):
    from flows import create_initial_node
    owner_email = tenant.get("notification_email") or tenant.get("email")

    async def send_email_task():
        if owner_email:
            try:
                conversation_summary = await generate_conversation_summary(flow_manager)
                await _send_lead_report_email(tenant, caller_phone, problem, details, urgency, owner_email, conversation_summary)
            except Exception as e:
                logger.error(f"📧 Lead email error: {e}")
        else:
            logger.warning("📧 No owner email for lead!")

    asyncio.create_task(send_email_task())

    # TTSSpeakFrame zamiast tuple — unikamy opóźnienia LLM przy powrocie do głównego node
    try:
        from pipecat.frames.frames import TTSSpeakFrame
        await flow_manager.task.queue_frame(TTSSpeakFrame(text=confirmation))
    except Exception as e:
        logger.warning(f"TTSSpeakFrame error: {e}")

    return (None, create_initial_node(tenant, greeting_played=True))


async def _send_lead_report_email(tenant: dict, caller_phone: str, problem: str, details: str, urgency: str, to_email: str, conversation_summary: str = ""):
    """Wyślij strukturalny email ze zgłoszeniem serwisowym"""
    import httpx, os
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt

    resend_api_key = os.getenv("RESEND_API_KEY")
    if not resend_api_key:
        return

    business_name = tenant.get("name", "Firma")
    now = _dt.now(ZoneInfo("Europe/Warsaw"))
    date_str = now.strftime("%d.%m.%Y, %H:%M")

    is_urgent = urgency == "high"
    urgency_badge = "🔴 PILNE" if is_urgent else "🟡 Normalne"
    urgency_color = "#dc2626" if is_urgent else "#d97706"
    urgency_bg = "#fef2f2" if is_urgent else "#fefce8"
    subject_prefix = "🔴 PILNE zgłoszenie" if is_urgent else "🔧 Nowe zgłoszenie"

    details_row = f"""
        <tr>
            <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; color: #64748b; font-size: 13px; width: 110px;">Szczegóły</td>
            <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; font-size: 14px;">{details}</td>
        </tr>""" if details else ""

    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: #1a1a2e; color: white; padding: 20px 25px; border-radius: 12px 12px 0 0;">
            <h2 style="margin: 0; font-size: 18px;">🔧 Nowe zgłoszenie serwisowe</h2>
            <p style="margin: 5px 0 0; opacity: 0.8; font-size: 13px;">{business_name} • {date_str}</p>
        </div>
        <div style="background: white; padding: 25px; border: 1px solid #e5e7eb; border-top: none;">
            <div style="background: {urgency_bg}; border-left: 4px solid {urgency_color}; padding: 12px 15px; border-radius: 0 8px 8px 0; margin-bottom: 20px;">
                <span style="font-weight: 700; color: {urgency_color}; font-size: 15px;">{urgency_badge}</span>
            </div>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <tr>
                    <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; color: #64748b; font-size: 13px; width: 110px;">Telefon</td>
                    <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; font-size: 14px;">
                        <a href="tel:{caller_phone}" style="color: #3b82f6; text-decoration: none; font-weight: 600;">{caller_phone}</a>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; color: #64748b; font-size: 13px;">Problem</td>
                    <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; font-size: 14px; font-weight: 500;">{problem}</td>
                </tr>
                {details_row}
                <tr>
                    <td style="padding: 10px 0; color: #64748b; font-size: 13px;">Data</td>
                    <td style="padding: 10px 0; font-size: 14px;">{date_str}</td>
                </tr>
            </table>
            {f'''<div style="background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 16px; margin-top: 4px;">
                <p style="margin: 0 0 6px; color: #64748b; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Streszczenie rozmowy</p>
                <p style="margin: 0; font-size: 14px; color: #374151; line-height: 1.5;">{conversation_summary}</p>
            </div>''' if conversation_summary else ''}
        </div>
        <div style="padding: 15px 25px; background: #f8fafc; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 12px 12px;">
            <p style="margin: 0; color: #94a3b8; font-size: 11px; text-align: center;">Zgłoszenie przekazane przez asystenta głosowego BizVoice.pl</p>
        </div>
    </div>"""

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": "Voice AI <noreply@bizvoice.pl>",
                    "to": [to_email],
                    "subject": f"{subject_prefix} — {caller_phone} — {business_name}",
                    "html": html_content,
                },
                timeout=10.0,
            )
            if resp.status_code == 200:
                logger.info(f"📧 Lead email sent to {to_email}")
            else:
                logger.error(f"📧 Lead email error: {resp.status_code} — {resp.text}")
    except Exception as e:
        logger.error(f"📧 Lead email send error: {e}")


__all__ = [
    "contact_owner_function",
    "do_transfer_function",
    "do_message_function",
    "set_contact_name_function",
    "set_contact_message_function",
    "create_contact_choice_node",
    "create_collect_contact_name_node",
    "create_collect_message_content_node",
    "submit_lead_function",
]