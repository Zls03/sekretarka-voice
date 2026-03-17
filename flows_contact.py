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
- klient chce się umówić ale rezerwacje są wyłączone (zaproponuj wiadomość)""",
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
        if existing_name:
            return (None, create_collect_message_content_node(tenant))
        else:
            return (None, create_collect_contact_name_node(tenant))
    
    # POTEM sprawdź czy chce TRANSFER
    transfer_keywords = ["połącz", "przekieruj", "bezpośrednio", "człowiek", "rozmawiać z"]
    wants_transfer = any(kw in reason for kw in transfer_keywords)
    
    if wants_transfer:
        if has_transfer:
            logger.info(f"📞 AUTO-TRANSFER based on reason keywords")
            return await execute_transfer(flow_manager, tenant)
        else:
            logger.info(f"📞 Transfer requested but DISABLED - offering message")
            return ("Niestety nie mogę teraz połączyć bezpośrednio, ale chętnie przekażę wiadomość do właściciela.", 
                    create_collect_contact_name_node(tenant))
    elif has_transfer:
        return (None, create_contact_choice_node(tenant))
    else:
        if existing_name:
            return (None, create_collect_message_content_node(tenant))
        else:
            return (None, create_collect_contact_name_node(tenant))
# ============================================================================
# NODE: Wybór - wiadomość czy połączenie (gdy transfer dostępny)
# ============================================================================

def create_contact_choice_node(tenant: dict) -> dict:
    """Pytanie o wybór - DWIE osobne funkcje (prostsze dla GPT)"""
    return {
        "name": "contact_choice",
        "pre_actions": [
            {"type": "tts_say", "text": "Mogę przekazać wiadomość do właściciela lub połączyć bezpośrednio. Co Pan woli?"}
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
    if flow_manager.state.get("contact_name"):
        return (None, create_collect_message_content_node(tenant))
    else:
        return (None, create_collect_contact_name_node(tenant))


# ============================================================================
# NODE: Zbieranie imienia
# ============================================================================

def create_collect_contact_name_node(tenant: dict) -> dict:
    """Zbierz imię klienta"""
    return {
        "name": "collect_contact_name",
        "pre_actions": [
            {"type": "tts_say", "text": "Dobrze, przekażę wiadomość. Jak się Pan nazywa?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": "Zbierasz imię klienta do wiadomości dla właściciela."
        }],
        "task_messages": [{
            "role": "system",
            "content": """Klient poda imię lub nazwisko.

Gdy powie jakiekolwiek imię/nazwisko → wywołaj set_contact_name
Tylko gdy WYRAŹNIE rezygnuje ("nie chcę", "nieważne", "do widzenia") → end_conversation
NIE interpretuj imienia jako pożegnania.
NIE powtarzaj imienia klienta w odpowiedzi — po prostu wywołaj funkcję."""
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
        return ({"status": "error", "message": "Nie dosłyszałam. Jak mogę zapisać?"}, 
                create_collect_contact_name_node(tenant))
    
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

def create_collect_message_content_node(tenant: dict) -> dict:
    return {
        "name": "collect_message_content",
        "pre_actions": [
            {"type": "tts_say", "text": "Co mam przekazać właścicielowi?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": "Zbierasz treść wiadomości dla właściciela."
        }],
        "task_messages": [{
            "role": "system",
            "content": """Klient poda treść wiadomości dla właściciela.

ZAWSZE wywołuj set_contact_message z dokładnie tym co klient powiedział.
Nawet jeśli wiadomość jest krótka lub dziwna - zapisz ją dosłownie.
NIE oceniaj treści. NIE interpretuj jako pożegnanie.
NIE odpowiadaj tekstem — TYLKO wywołaj funkcję."""
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
        return ({"status": "error", "message": "Nie dosłyszałam. Co mam przekazać?"}, 
                create_collect_message_content_node(tenant))
    
    name = flow_manager.state.get("contact_name", "Nieznany")
    
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
    
    return (f"Wiadomość przekazana. Salon oddzwoni.",
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
                create_collect_contact_name_node(tenant))
    
    call_sid = flow_manager.state.get("call_sid")
    
    if not call_sid:
        logger.error("📞 No call_sid for transfer!")
        return ("Przepraszam, wystąpił problem. Czy mogę przekazać wiadomość?",
                create_collect_contact_name_node(tenant))
    
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
                create_collect_contact_name_node(tenant))
    
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
                create_collect_contact_name_node(tenant))
    
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
    
    return ("Łączę z właścicielem, proszę chwilę poczekać.", _create_transfer_end_node())


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

__all__ = [
    "contact_owner_function",
    "do_transfer_function",
    "do_message_function",
    "set_contact_name_function", 
    "set_contact_message_function",
    "create_contact_choice_node",
    "create_collect_contact_name_node",
    "create_collect_message_content_node",
]