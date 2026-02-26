import json
import random
from plugins_func.register import register_function, ToolType, ActionResponse, Action


# --- Function description ---
GET_ADVICE_FOR_EMOTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_advice_for_emotion",
        "description": (
            "ACT AS AN EMOTIONAL SENSOR. Call this tool when you detect a 'Vulnerability Spike' where the user needs a companionship-based intervention. Trigger it if the user expresses vulnerability, self-doubt, or social friction, even if hidden behind humor or casual language. Focus on signals like 'paralysis' (e.g., 'I can't even...'), 'safety-seeking' (e.g., 'I just want to hide...'), or 'diminishing' (e.g., 'it's just stupid but...'). Your goal is to match the tone of the user's struggle to the best coping category. Do NOT trigger for simple facts, happy updates, or casual venting without vulnerability."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "emotion": {
                    "type": "string",
                    "description": (
                        "The specific state you've sensed. Map the user's subtext to one of the allowed categories. "
                        "Category guide: "
                        "anxiety — Signaled by repetitive 'what-if' loops; catastrophic future-thinking; mentions of a 'noisy' or 'crowded' mind. Phrases: 'I can't stop thinking about...', 'What if they hate me?', 'My mind won't shut up.' DO NOT trigger if user is just excited/nervous for a positive event (e.g., a concert). "
                        "stress — Signaled by mentions of 'external weight,' deadlines, or feeling 'stretched thin' by chores/school. Phrases: 'I have so much to do,' 'I'm exhausted from school,' 'It's all piling up.' DO NOT trigger if the user describes feeling 'frozen' (that's overwhelm). "
                        "overwhelm — Signaled by feelings of paralysis, being 'stuck,' unable to start tasks, or mentions of sensory saturation (too loud/bright). Phrases: 'Everything is too much,' 'I'm just staring at my room,' 'I can't even pick one thing.' DO NOT trigger if the user is just venting about a busy day or successfully multitasking or being productive without expressing paralysis. "
                        "sadness — Signaled by feelings of heaviness, low energy, or mentions of crying. Phrases: 'I just want to cry,' 'I feel so heavy,' 'Nothing is fun anymore.' DO NOT trigger for boredom or tiredness without emotional weight. "
                        "social_anxiety — Signaled by fear of judgment, mentions of 'masking' or 'hiding' in social situations, or anticipatory dread about interactions. Phrases: 'I just want to hide,' 'Everyone is looking at me,' 'They think I'm weird.' DO NOT trigger if venting about a specific mean person. "
                        "panic — Signaled by mentions of intense fear, physical symptoms (racing heart, shortness of breath), or feelings of impending doom. Phrases: 'I feel like I'm dying,' 'My heart is pounding,' 'I can't breathe.' DO NOT trigger for general worries in the future with no immediate impact or physical discomfort. "
                        "loneliness — Signaled by feelings of isolation, mentions of 'nobody understands me,' or longing for connection. Phrases: 'I feel so alone,' 'Nobody wants to hang out,' 'I just want someone to talk to.' DO NOT trigger if the user states they are at home alone or express a positive desire for solitude. "
                        "insecurity — Signaled by self-doubt, mentions of 'not good enough,' or comparing oneself negatively to others. Phrases: 'I wish I was more like them,' 'I'm not good at anything.' DO NOT trigger for minor 'I'm bad at this game' or I made a small mistake comments. "
                        "identity_doubt — Signaled by existential questioning, mentions of 'being a fake character,' or feeling like an imposter. Phrases: 'I don't even know who I am,' 'I feel like a fraud,' 'Maybe I'm just a fake.' DO NOT trigger for fashion/aesthetic changes or changes in interests. "
                        "fandom_shame — Signaled by embarrassment about one's interests, mentions of being 'cringe' for liking something, and an apologetic tone for enjoying a hobby. Phrases: 'I wish I didn't like this,' 'don't laugh but I like,' 'People would make fun of me if they knew.' DO NOT trigger for geeking out with high energy or joking about being a 'basic fan.' "
                        "general — Vulnerability detected but doesn't fit a specific category above."
                    ),
                    "enum": [
                        "anxiety",
                        "stress",
                        "overwhelm",
                        "sadness",
                        "social_anxiety",
                        "panic",
                        "loneliness",
                        "insecurity",
                        "identity_doubt",
                        "fandom_shame",
                        "general",
                    ],
                }
            },
            "required": ["emotion"],
        },
    },
}


# --- Curated coping library ---
COPING_LIBRARY = {
    "anxiety": [
        "Try a 'Physiological Sigh': Two quick inhales through the nose, then one long exhale through the mouth.",
        "Look around and name 5 things you can see right now. Take your time.",
        "Wiggle your toes inside your shoes and focus on that feeling for 30 seconds.",
        "Try '4-7-8 breathing': In for 4, hold for 7, out for 8.",
        "Find a distant object and stare at it, noticing every small detail of its shape.",
        "Hum a low, steady note and feel the vibration in your throat."
    ],
    "stress": [
        "Reach your arms up to the ceiling, stretch as wide as you can, then let them drop.",
        "Roll your shoulders backward three times, then forward three times.",
        "Take a slow sip of water and feel it moving all the way down.",
        "Squeeze your hands into tight fists, hold for 5 seconds, and release.",
        "Press your palms together firmly and notice the strength in your arms.",
        "Slowly turn your head to the left, then to the right, as far as is comfortable."
    ],
    "overwhelm": [
        "Close your eyes and count five slow breaths. Just five.",
        "Touch something nearby that is very cold or very soft. Describe it in your head.",
        "Put your phone face-down and sit still for exactly 60 seconds.",
        "Gently tap your collarbone with your fingertips in a slow, steady beat.",
        "Pick one small object near you and describe its color and texture to yourself.",
        "Let your jaw go slack and let your tongue rest away from the roof of your mouth."
    ],
    "sadness": [
        "Wrap a soft blanket or a hoodie around your shoulders as tight as you like.",
        "Gently rub your own arms or shoulders for a moment of comfort.",
        "Wash your face with lukewarm water and pat it dry very slowly.",
        "Listen to one peaceful song from start to finish without doing anything else.",
        "Rest your eyes for a minute by looking at something green or natural nearby.",
        "Take a very slow, deep breath and imagine it filling up your whole chest."
    ],
    "social_anxiety": [
        "Exhale slowly, like you're blowing out a tiny candle across the room.",
        "Find a pattern in the room (like a rug or a curtain) and trace it with your eyes.",
        "Press your feet firmly into the floor and feel how steady the ground is.",
        "Notice the feeling of your clothes against your skin for a moment.",
        "Briefly tense your calf muscles, hold, and then let them go completely.",
        "Find three blue things in the room and name them quietly to yourself."
    ],
    "panic": [
        "Splash ice-cold water on your face or wrists. It helps reset your system.",
        "Push your hands against a wall as hard as you can for 10 seconds, then relax.",
        "Name 5 fruits, going in alphabetical order: Apple, Banana, Cherry...",
        "Press your back against a wall and feel the solid support behind you.",
        "Count backward from 20 by 3s: 20, 17, 14, 11...",
        "Gently pinch the skin between your thumb and index finger for a few seconds."
    ],
    "loneliness": [
        "Hold a warm cup or a warm water bottle and feel the heat in your palms.",
        "Write down the name of one person, pet, or character you've always liked.",
        "Pet something soft nearby—like a pillow, a plushie, or a soft fabric.",
        "Step outside or open a window to feel the air move across your face.",
        "Look at a photo of a place you love and find three small details in it.",
        "Take a deep breath and feel the air filling up your lungs."
    ],
    "insecurity": [
        "You don't have to be the best version of yourself every single day.",
        "Think of one person who would disagree with that inner critic right now.",
        "Feeling not good enough is one of the most universal human experiences — it doesn't make it true.",
        "Name one small thing you did today that took effort, even if nobody noticed.",
        "The people who matter don't need you to be perfect.",
        "You're comparing your behind-the-scenes to everyone else's highlight reel."
    ],
    "identity_doubt": [
        "Not knowing who you are yet isn't a flaw — it means you're still growing.",
        "You don't have to have it all figured out. Nobody actually does.",
        "The fact that you're questioning means you care about being authentic.",
        "Who you are today doesn't have to be who you are forever, and that's okay.",
        "Try finishing this sentence: 'One thing I know for sure about myself is...'",
        "Your identity isn't a test with a right answer — it's something you get to explore."
    ],
    "fandom_shame": [
        "The things you love are part of what makes you you. That's worth protecting.",
        "Liking what you like doesn't need anyone else's permission.",
        "The most interesting people are the ones who care deeply about something specific.",
        "Think about how it feels when you're enjoying it with no one watching — that feeling is real.",
        "Every fandom started with one person who wasn't embarrassed to love it first.",
        "Your interests don't have to make sense to everyone. They just have to make sense to you."
    ],
    "general": [
        "Take three big, deep 'plushie' breaths. In... and out.",
        "Stretch your fingers out as wide as they go, then make a soft fist.",
        "Find a quiet spot and close your eyes for five slow heartbeats.",
        "Gently roll your head from side to side to relax your neck.",
        "Shake out your hands and feet for 10 seconds to let go of extra energy.",
        "Yawn as wide as you can, even if you have to fake it at first."
    ]
}


@register_function("get_advice_for_emotion", GET_ADVICE_FOR_EMOTION_DESC, ToolType.WAIT)
def get_advice_for_emotion(emotion: str = "general"):
    """Pick a random coping suggestion from the curated library for the given emotion."""
    emotion = emotion.lower().strip()
    suggestions = COPING_LIBRARY.get(emotion, COPING_LIBRARY["general"])
    suggestion = random.choice(suggestions)
    return ActionResponse(
        action=Action.REQLLM,
        result=json.dumps(
            {"emotion": emotion, "suggestion": suggestion}, ensure_ascii=False
        ),
        response=None,
    )
