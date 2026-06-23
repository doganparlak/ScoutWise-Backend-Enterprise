# ======== CHATBOT.PY MODULE: COST APPROXIMATION HELPERS ========
# === Cost model constants ===
# DeepSeek Chat pricing (cache miss) – per token
DEEPSEEK_INPUT_PRICE_PER_TOKEN = 0.28 / 1_000_000.0   # $0.28 / 1M input
DEEPSEEK_OUTPUT_PRICE_PER_TOKEN = 0.42 / 1_000_000.0  # $0.42 / 1M output

# OpenAI text-embedding-3-small pricing – per token
EMBEDDING_PRICE_PER_TOKEN = 0.02 / 1_000_000.0        # $0.02 / 1M tokens

def estimate_tokens(text: str) -> int:
    """
    Very rough token estimator: ~4 characters per token.
    This is approximate but good enough for ballpark cost logging.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)

# Example player row (used to approximate weekly DB embedding refresh cost)
EXAMPLE_PLAYER_ROW_TEXT = """player_name: Danilo
gender: male
height: 184.0
weight: 78.0
age: 34.0
match_count: 2
nationality_name: Brazil
team_name: Flamengo
Accurate Crosses: 0.5
Accurate Passes: 49.5
Accurate Passes (%): 46.5
Aerials: 1.5
Aerials Won (%): 33.5
Aeriels Lost: 0.5
Aeriels Won: 1.0
Backward Passes: 3.5
Ball Recovery: 3.0
Big Chances Missed: 0.5
Clearances: 1.0
Dribbled Past: 0.5
Duels Lost: 0.5
Duels Won: 1.0
Duels Won (%): 25.0
Fouls Drawn: 0.5
Interceptions: 0.5
Key Passes: 1.0
Long Balls: 4.5
Long Balls Won: 2.0
Long Balls Won (%): 20.0
Minutes Played: 45.0
Passes: 53.5
Passes In Final Third: 10.5
Possession Lost: 4.5
Rating: 3.775
Shots Off Target: 0.5
Shots Total: 0.5
Successful Crosses (%): 25.0
Tackles: 0.5
Tackles Won: 0.5
Tackles Won (%): 50.0
Total Crosses: 1.5
Total Duels: 2.0
Touches: 57.0
"""

def estimate_weekly_db_embedding_cost() -> None:
    """
    Estimate weekly embedding refresh cost for the player DB.

    Assumptions (adjust in code if needed):
    - 113 leagues
    - 20 teams per league
    - 25 players per team
    - One row per player
    - Embeddings: text-embedding-3-small at $0.02 / 1M tokens
    """
    LEAGUES = 113
    TEAMS_PER_LEAGUE = 20
    PLAYERS_PER_TEAM = 25

    num_players = LEAGUES * TEAMS_PER_LEAGUE * PLAYERS_PER_TEAM
    tokens_per_row = estimate_tokens(EXAMPLE_PLAYER_ROW_TEXT)
    total_tokens = num_players * tokens_per_row
    total_cost = total_tokens * EMBEDDING_PRICE_PER_TOKEN

    print(
        "[COST] Weekly DB embedding refresh (approx): "
        f"{num_players} players, ~{tokens_per_row} tokens/row, "
        f"total_tokens≈{total_tokens}, cost≈${total_cost:.4f}"
    )


