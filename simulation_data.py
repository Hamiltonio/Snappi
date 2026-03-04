"""
Temporary simulation data for testing yesterday's Champions League fixtures.
Mimics api-sports.io responses so main.py can run in simulation mode.

Fixture 1: Olympiacos vs Leverkusen — Final 0-2. At 70': 4 DA, 2 Shots (low pressure).
Fixture 2: Club Brugge vs Atl. Madrid — Final 3-3. At 70': 3 DA, 1 Shot (low pressure).

Use with run_simulation.py to force 3:45 PM and process 70' window, then run nightly_analysis().
"""

# Fixture IDs used only in simulation (avoid collision with real API)
FIXTURE_OLYMPIAKOS_LEVERKUSEN = 10001
FIXTURE_BRUGGE_ATLETICO = 10002


def get_live_fixtures_simulation():
    """
    Mock GET /fixtures?live=all: two fixtures at 70' with low score (so they pass 70' filter).
    Olympiacos 0-1 at 70', Brugge 1-1 at 70'.
    """
    return [
        {
            "fixture": {"id": FIXTURE_OLYMPIAKOS_LEVERKUSEN, "status": {"elapsed": 70}},
            "goals": {"home": 0, "away": 1},
            "teams": {
                "home": {"name": "Olympiacos"},
                "away": {"name": "Leverkusen"},
            },
        },
        {
            "fixture": {"id": FIXTURE_BRUGGE_ATLETICO, "status": {"elapsed": 70}},
            "goals": {"home": 1, "away": 1},
            "teams": {
                "home": {"name": "Club Brugge"},
                "away": {"name": "Atl. Madrid"},
            },
        },
    ]


def get_fixture_statistics_simulation(fixture_id: int):
    """
    Mock GET /fixtures/statistics: return sum of both teams.
    Olympiacos vs Leverkusen: 4 Dangerous Attacks, 2 Shots.
    Club Brugge vs Atl. Madrid: 3 Dangerous Attacks, 1 Shot.
    """
    if fixture_id == FIXTURE_OLYMPIAKOS_LEVERKUSEN:
        return {"total_shots": 2, "dangerous_attacks": 4}
    if fixture_id == FIXTURE_BRUGGE_ATLETICO:
        return {"total_shots": 1, "dangerous_attacks": 3}
    return None


def get_fixture_result_simulation(fixture_id: int):
    """
    Mock GET /fixtures?id=X: final score for nightly_analysis().
    Olympiacos vs Leverkusen: 0-2 (win for us: under).
    Club Brugge vs Atl. Madrid: 3-3 (loss: 6 goals >= 3).
    """
    if fixture_id == FIXTURE_OLYMPIAKOS_LEVERKUSEN:
        return {
            "home_name": "Olympiacos",
            "away_name": "Leverkusen",
            "home_goals": 0,
            "away_goals": 2,
        }
    if fixture_id == FIXTURE_BRUGGE_ATLETICO:
        return {
            "home_name": "Club Brugge",
            "away_name": "Atl. Madrid",
            "home_goals": 3,
            "away_goals": 3,
        }
    return None
