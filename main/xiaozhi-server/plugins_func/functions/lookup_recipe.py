from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action

TAG = __name__
logger = setup_logging()

LOOKUP_RECIPE_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "lookup_recipe",
        "description": (
            "Return a simple complete recipe for a named dish. "
            "Use clear step by step instructions that sound natural when spoken. "
            "Avoid special symbols or formatting that may break text to speech."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dish": {
                    "type": "string",
                    "description": "Name of the dish the user wants to cook",
                },
            },
            "required": ["dish"],
        },
    },
}


def _normalize_dish(dish: str) -> str:
    return " ".join(dish.lower().strip().split())


def _get_recipe(dish_key: str) -> dict:
    """
    V0 built in recipe library.
    Keep recipes short, spoken friendly, and deterministic.
    """

    if dish_key in ("fried rice", "egg fried rice"):
        return {
            "title": "Fried rice",
            "ingredients": [
                "Two cups cooked rice, preferably cold",
                "Two eggs",
                "Two tablespoons cooking oil",
                "One cup mixed vegetables",
                "Two tablespoons soy sauce",
                "Salt and pepper to taste",
            ],
            "steps": [
                "Break up the cooked rice so it is not clumped together",
                "Heat a pan on medium high heat and add the oil",
                "Crack the eggs into the pan and scramble them",
                "Remove the eggs and set them aside",
                "Add the vegetables to the pan and cook for two to three minutes",
                "Add the rice and stir fry until heated through",
                "Add soy sauce and mix well",
                "Add the eggs back in and stir everything together",
                "Taste and season with salt and pepper",
            ],
            "notes": [
                "Cold rice works best for fried rice",
                "You can add chicken shrimp or tofu if you like",
            ],
        }

    if dish_key in ("crepes", "crêpes"):
        return {
            "title": "Crepes",
            "ingredients": [
                "One cup flour",
                "Two eggs",
                "One and a quarter cups milk",
                "Two tablespoons melted butter or oil",
                "A pinch of salt",
            ],
            "steps": [
                "Add flour and salt to a bowl and mix",
                "Add eggs and whisk until smooth",
                "Slowly add milk while whisking to avoid lumps",
                "Mix in the melted butter",
                "Heat a non stick pan on medium heat and lightly grease it",
                "Pour in a small amount of batter and swirl to form a thin layer",
                "Cook until the edges lift then flip and cook briefly",
                "Repeat with remaining batter",
            ],
            "notes": [
                "Serve with fruit or jam for sweet crepes",
                "Serve with cheese or eggs for savory crepes",
            ],
        }

    # Generic fallback recipe
    return {
        "title": dish_key.title(),
        "ingredients": [
            "Main ingredient for the dish",
            "Cooking oil or butter",
            "Salt and pepper",
            "Any spices or sauce typical for this dish",
        ],
        "steps": [
            "Prepare and measure all ingredients",
            "Heat a pan or pot on medium heat and add oil",
            "Add the main ingredients and cook until done",
            "Season to taste and stir well",
            "Serve while warm",
        ],
        "notes": [
            "Tell me what ingredients you have and I can customize this recipe",
        ],
    }


@register_function("lookup_recipe", LOOKUP_RECIPE_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def lookup_recipe(conn, dish: str):
    """
    Lookup a simple spoken recipe for a dish.
    Returns structured facts and lets the LLM render the final response.
    """

    if not dish or not dish.strip():
        logger.bind(tag=TAG).warning("lookup_recipe called without dish")
        return ActionResponse(
            Action.REQLLM,
            "Please tell me the name of the dish you want to cook",
            None,
        )

    dish_key = _normalize_dish(dish)
    recipe = _get_recipe(dish_key)

    # Build spoken friendly text
    response_text = f"Here is a simple recipe for {recipe['title']}.\n\n"

    response_text += "Ingredients:\n"
    for item in recipe["ingredients"]:
        response_text += f"{item}\n"

    response_text += "\nSteps:\n"
    for idx, step in enumerate(recipe["steps"], start=1):
        response_text += f"Step {idx}. {step}\n"

    if recipe.get("notes"):
        response_text += "\nNotes:\n"
        for note in recipe["notes"]:
            response_text += f"{note}\n"

    logger.bind(tag=TAG).info(f"lookup_recipe served dish={dish}")

    return ActionResponse(Action.REQLLM, response_text, None)
