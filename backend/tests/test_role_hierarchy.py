from app.chat.hierarchy import role_rank
from app.db.models import Role

DOMAIN = "alpha.localhost"


def role(role_id: int, position: int) -> Role:
    return Role(
        id=role_id,
        origin_domain=DOMAIN,
        guild_id=1,
        guild_domain=DOMAIN,
        name="test",
        permissions=0,
        position=position,
    )


def test_higher_position_outranks_lower_position() -> None:
    assert role_rank(role(50, 2)) > role_rank(role(10, 1))


def test_lower_snowflake_wins_equal_position() -> None:
    assert role_rank(role(10, 2)) > role_rank(role(50, 2))
