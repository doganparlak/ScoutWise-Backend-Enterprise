from __future__ import annotations

from dataclasses import dataclass

from api_module import main


@dataclass
class _FirstResult:
    row: dict | None

    def mappings(self) -> "_FirstResult":
        return self

    def first(self) -> dict | None:
        return self.row


class _StableIdSession:
    def __init__(self) -> None:
        self.params: list[dict] = []

    def execute(self, _statement, params):
        self.params.append(params)
        if params.get("player_id") == 8_417_851:
            return _FirstResult(
                {
                    "id": 7182,
                    "metadata": {
                        "player_id": 8_417_851,
                        "player_name": "Gabriel Sara",
                    },
                }
            )
        raise AssertionError(f"Unexpected identity lookup: {params}")


def test_report_resolution_prefers_sportmonks_id_over_reused_row_id():
    db = _StableIdSession()

    row = main._resolve_enterprise_player_pool_report_club_row(
        db,
        {
            "sportmonksPlayerId": 8_417_851,
            "club_player_id": 8075,
            "name": "Gabriel Sara",
        },
    )

    assert row["id"] == 7182
    assert db.params == [{"player_id": 8_417_851}]


def test_favorite_snapshot_carries_stable_sportmonks_identity():
    identity = main._enterprise_favorite_identity(
        {
            "id": "favorite-id",
            "player_id": "8417851",
            "club_player_id": 8075,
            "name": "Gabriel Sara",
        }
    )

    assert identity["player_id"] == "8417851"
    assert identity["sportmonksPlayerId"] == 8_417_851
    assert identity["club_player_id"] == 8075


def test_club_row_refresh_persists_stable_sportmonks_identity(monkeypatch):
    monkeypatch.setattr(
        main,
        "reveal_player_potential",
        lambda *_args, **_kwargs: {"potential": 70},
    )
    monkeypatch.setattr(
        main,
        "reveal_player_form",
        lambda *_args, **_kwargs: {"form": 71},
    )

    payload = main._apply_enterprise_club_row_to_report_payload(
        object(),
        {"name": "Gabriel Sara", "club_player_id": 8075},
        {
            "id": 7182,
            "metadata": {
                "player_id": 8_417_851,
                "player_name": "Gabriel Sara",
            },
        },
    )

    assert payload["sportmonksPlayerId"] == 8_417_851
    assert payload["player_id"] == "8417851"
    assert payload["club_player_id"] == 7182
