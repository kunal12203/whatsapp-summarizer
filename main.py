from fastapi import FastAPI, Request, Form
from twilio.rest import Client
from datetime import datetime, timedelta
import anthropic
import os
from dotenv import load_dotenv
from collections import defaultdict
import re
import json

load_dotenv()

app = FastAPI()

# Initialize clients
twilio_client = Client(
    os.getenv('TWILIO_ACCOUNT_SID'),
    os.getenv('TWILIO_AUTH_TOKEN')
)
anthropic_client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# Data storage
group_messages = defaultdict(list)  # {group_id: [messages]}
user_last_read = defaultdict(dict)  # {user_phone: {group_id: timestamp}}

# Token limits
MAX_TOKENS_PER_SUMMARY = 8000
ESTIMATED_CHARS_PER_TOKEN = 4


@app.post("/webhook")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
    ProfileName: str = Form(None),
    To: str = Form(...),
    WaId: str = Form(None),
    # Group-specific fields (available with Business API)
    Author: str = Form(None),  # Person who sent message in group
    GroupId: str = Form(None)  # WhatsApp group ID
):
    """
    Receives all WhatsApp messages from groups and DMs
    """
    
    user_phone = From
    message_text = Body.strip()
    sender_name = ProfileName or "Unknown"
    author = Author or user_phone  # Author is set for group messages
    group_id = GroupId or user_phone  # GroupId is set for group messages
    
    print(f"📱 Group: {group_id} | From: {sender_name} ({author}): {message_text}")
    
    # Check if this is a group message (has GroupId)
    is_group_message = GroupId is not None
    
    # Check if bot is mentioned
    bot_mentioned = is_bot_mentioned(message_text)
    
    if bot_mentioned and is_group_message:
        # Extract command and handle in group
        command = remove_bot_mention(message_text)
        await handle_group_command(group_id, author, sender_name, command)
    
    elif is_group_message:
        # Store regular group message
        store_group_message(group_id, author, sender_name, message_text)
    
    else:
        # DM to bot - handle help commands
        handle_dm_command(user_phone, message_text)
    
    return {"status": "received"}


def is_bot_mentioned(message: str) -> bool:
    """Check if bot is mentioned"""
    message_lower = message.lower()
    bot_triggers = ['@bot', '@summarizer', 'hey bot', 'bot summarize', 'summarize']
    
    return any(trigger in message_lower for trigger in bot_triggers)


def remove_bot_mention(message: str) -> str:
    """Remove bot mention to get actual command"""
    message = re.sub(r'@\w+\s*', '', message, flags=re.IGNORECASE)
    message = re.sub(r'\bbot\b\s*', '', message, flags=re.IGNORECASE)
    return message.strip()


async def handle_group_command(group_id: str, author: str, sender_name: str, command: str):
    """
    Handle command in the group itself
    Mentions the person who requested it
    """
    
    # Parse the command using Claude
    intent = await parse_command_intent(command)
    
    if intent['action'] == 'summarize':
        time_filter = intent.get('time_filter', 'all')
        from_last_read = intent.get('from_last_read', False)
        
        # Generate summary
        summary = generate_group_summary(
            group_id, 
            author, 
            time_filter, 
            from_last_read
        )
        
        # Reply in group mentioning the requester
        reply = f"@{sender_name}\n\n{summary}"
        send_group_message(group_id, reply)
        
        # Update last read for this user
        user_last_read[author][group_id] = datetime.now()
    
    else:
        send_group_message(group_id, 
            f"@{sender_name} I didn't understand that. Try:\n"
            "• 'summarize today's chat'\n"
            "• 'summarize from last read'\n"
            "• 'catch me up on last 2 hours'")


async def parse_command_intent(command: str) -> dict:
    """Use Claude to parse natural language command"""
    
    try:
        response = anthropic_client.messages.create(
            model="claude-3-5-haiku",
            max_tokens=200,
            temperature=0,
            system="""You are a command parser for a WhatsApp summarizer bot.
Parse the user's command and return ONLY a JSON object:
{
    "action": "summarize|help|unknown",
    "time_filter": "today|last_hour|last_2_hours|last_day|all",
    "from_last_read": true|false
}

Examples:
"summarize today's chat" -> {"action": "summarize", "time_filter": "today", "from_last_read": false}
"catch me up from where I left" -> {"action": "summarize", "time_filter": "all", "from_last_read": true}
"what happened in last 2 hours" -> {"action": "summarize", "time_filter": "last_2_hours", "from_last_read": false}
"summarize from my last read" -> {"action": "summarize", "time_filter": "all", "from_last_read": true}
"summarize" -> {"action": "summarize", "time_filter": "all", "from_last_read": true}
""",
            messages=[{
                "role": "user",
                "content": f"Parse this command: {command}"
            }]
        )
        
        intent_text = response.content[0].text.strip()
        intent_text = re.sub(r'```json\n?|\n?```', '', intent_text)
        intent = json.loads(intent_text)
        return intent
    
    except Exception as e:
        print(f"❌ Error parsing intent: {e}")
        return {"action": "unknown"}


def generate_group_summary(group_id: str, author: str, time_filter: str, from_last_read: bool) -> str:
    """
    Generate summary for the group
    """
    
    if group_id not in group_messages or not group_messages[group_id]:
        return "📝 No messages to summarize yet!"
    
    all_messages = group_messages[group_id]
    
    # Filter messages
    filtered_messages = filter_messages(all_messages, author, group_id, time_filter, from_last_read)
    
    if not filtered_messages:
        return f"📝 No messages found for '{time_filter}'"
    
    # Apply token limit
    filtered_messages = apply_token_limit(filtered_messages)
    
    # Generate summary
    summary = generate_summary(filtered_messages, time_filter, len(all_messages))
    
    msg_count = len(filtered_messages)
    time_info = get_time_range_text(time_filter, from_last_read)
    
    return f"📝 *Summary* ({msg_count} messages {time_info}):\n\n{summary}"


def filter_messages(messages: list, author: str, group_id: str, 
                   time_filter: str, from_last_read: bool) -> list:
    """Filter messages based on time and last read"""
    
    now = datetime.now()
    
    # Filter by last read if requested
    if from_last_read:
        last_read_time = user_last_read.get(author, {}).get(group_id)
        if last_read_time:
            messages = [m for m in messages if m['timestamp'] > last_read_time]
        else:
            # No previous read - get last 50 messages
            messages = messages[-50:] if len(messages) > 50 else messages
    
    # Filter by time
    if time_filter == "today":
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        messages = [m for m in messages if m['timestamp'] >= start_of_day]
    
    elif time_filter == "last_hour":
        cutoff = now - timedelta(hours=1)
        messages = [m for m in messages if m['timestamp'] >= cutoff]
    
    elif time_filter == "last_2_hours":
        cutoff = now - timedelta(hours=2)
        messages = [m for m in messages if m['timestamp'] >= cutoff]
    
    elif time_filter == "last_day":
        cutoff = now - timedelta(days=1)
        messages = [m for m in messages if m['timestamp'] >= cutoff]
    
    elif time_filter == "all" and not from_last_read:
        # Get last 100 messages
        messages = messages[-100:] if len(messages) > 100 else messages
    
    return messages


def apply_token_limit(messages: list) -> list:
    """Truncate messages to fit token limit (keeps most recent)"""
    
    total_chars = sum(len(m['sender']) + len(m['text']) for m in messages)
    estimated_tokens = total_chars / ESTIMATED_CHARS_PER_TOKEN
    
    if estimated_tokens <= MAX_TOKENS_PER_SUMMARY:
        return messages
    
    truncated = []
    current_tokens = 0
    
    for message in reversed(messages):
        msg_chars = len(message['sender']) + len(message['text'])
        msg_tokens = msg_chars / ESTIMATED_CHARS_PER_TOKEN
        
        if current_tokens + msg_tokens > MAX_TOKENS_PER_SUMMARY:
            break
        
        truncated.insert(0, message)
        current_tokens += msg_tokens
    
    print(f"⚠️ Truncated {len(messages)} → {len(truncated)} messages (token limit)")
    return truncated


def get_time_range_text(time_filter: str, from_last_read: bool) -> str:
    """Human-readable time range"""
    if from_last_read:
        return "since your last read"
    
    time_map = {
        "today": "from today",
        "last_hour": "from last hour",
        "last_2_hours": "from last 2 hours",
        "last_day": "from last 24 hours",
        "all": ""
    }
    return time_map.get(time_filter, "")


def generate_summary(messages: list, time_filter: str, total_messages: int) -> str:
    """Generate AI summary using Claude"""
    
    # Format messages
    chat_text = "\n".join([
        f"{m['sender']}: {m['text']}" 
        for m in messages
    ])
    
    input_tokens = len(chat_text) / ESTIMATED_CHARS_PER_TOKEN
    print(f"📊 Estimated input tokens: {int(input_tokens)}")
    
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            temperature=0.7,
            system="""You are a WhatsApp group summarizer. 
Create a concise, actionable summary focusing on:
- Key decisions or announcements
- Important questions (especially unanswered ones)
- Action items or tasks
- Notable discussions
- Urgent/time-sensitive info

Format:
- Use 3-7 bullet points
- Use WhatsApp formatting (*bold*, _italic_)
- Be conversational and highlight what matters
- If someone was asked a question, mention it clearly
- Keep it scannable and useful

Do NOT include greetings, small talk, or irrelevant chatter.""",
            messages=[{
                "role": "user",
                "content": f"Summarize this WhatsApp group chat:\n\n{chat_text}"
            }]
        )
        
        usage = response.usage
        print(f"📊 Tokens - Input: {usage.input_tokens}, Output: {usage.output_tokens}")
        
        return response.content[0].text.strip()
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return "❌ Failed to generate summary. Please try again."


def store_group_message(group_id: str, author: str, sender_name: str, text: str):
    """Store a group message"""
    
    group_messages[group_id].append({
        'author': author,
        'sender': sender_name,
        'text': text,
        'timestamp': datetime.now()
    })
    
    # Keep last 2000 messages per group
    if len(group_messages[group_id]) > 2000:
        group_messages[group_id] = group_messages[group_id][-2000:]
    
    print(f"💾 Stored message in {group_id}: {len(group_messages[group_id])} total")


def send_group_message(group_id: str, message: str):
    """Send message to the group"""
    try:
        twilio_client.messages.create(
            from_=os.getenv('TWILIO_WHATSAPP_NUMBER'),
            to=group_id,  # In Business API, this is the group's WhatsApp ID
            body=message
        )
        print(f"✅ Sent to group {group_id}")
    except Exception as e:
        print(f"❌ Failed to send to group: {e}")


def handle_dm_command(user_phone: str, message: str):
    """Handle DM commands (help, info, etc)"""
    
    if 'help' in message.lower() or message.strip() == '/help':
        help_text = """
🤖 *WhatsApp Group Summarizer Bot*

*How to use:*
1. Add me to your WhatsApp group
2. In the group, mention me with a command:

*Examples:*
- @bot summarize today's chat
- @bot summarize from my last read
- @bot catch me up on last 2 hours
- @bot what happened?

I'll reply directly in the group!

*Features:*
✅ Smart summaries (key points, questions, action items)
✅ Time-based filtering (today, last hour, etc)
✅ Tracks your last read position
✅ Token-aware (handles large chats)

Just mention me anytime in the group!
        """
        send_dm(user_phone, help_text.strip())
    
    else:
        send_dm(user_phone, 
            "👋 Hi! I'm a group summarizer bot.\n\n"
            "Add me to your WhatsApp groups and mention me:\n"
            "'@bot summarize today's chat'\n\n"
            "Type 'help' for more info!")


def send_dm(to_phone: str, message: str):
    """Send DM to individual user"""
    try:
        twilio_client.messages.create(
            from_=os.getenv('TWILIO_WHATSAPP_NUMBER'),
            to=to_phone,
            body=message
        )
        print(f"✅ Sent DM to {to_phone}")
    except Exception as e:
        print(f"❌ Failed to send DM: {e}")


@app.get("/")
async def root():
    return {"status": "WhatsApp Summarizer Bot is running!"}


@app.get("/health")
async def health():
    """Health check with stats"""
    total_messages = sum(len(msgs) for msgs in group_messages.values())
    total_users = len(user_last_read)
    
    return {
        "status": "healthy",
        "total_groups": len(group_messages),
        "total_messages": total_messages,
        "total_users_tracking": total_users,
        "token_limit": MAX_TOKENS_PER_SUMMARY
    }


@app.get("/stats/{group_id}")
async def group_stats(group_id: str):
    """Get stats for a specific group"""
    if group_id not in group_messages:
        return {"error": "Group not found"}
    
    messages = group_messages[group_id]
    senders = {}
    
    for msg in messages:
        sender = msg['sender']
        senders[sender] = senders.get(sender, 0) + 1
    
    return {
        "group_id": group_id,
        "total_messages": len(messages),
        "unique_senders": len(senders),
        "top_senders": sorted(senders.items(), key=lambda x: x[1], reverse=True)[:5],
        "oldest_message": messages[0]['timestamp'].isoformat() if messages else None,
        "newest_message": messages[-1]['timestamp'].isoformat() if messages else None
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)