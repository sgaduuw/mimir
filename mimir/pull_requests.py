"""The original pull-request subject predicate shared by SQL and listings."""

PULL_PREFIX = "[GIT PULL]"
PULL_LIKE = PULL_PREFIX + "%"


def pull_subject_sql(column):
    return column.ilike(PULL_LIKE)


def is_pull_subject(subject: str | None) -> bool:
    # SQLite LIKE folds ASCII only, so Python must do the same.
    return (subject or "")[: len(PULL_PREFIX)].translate(
        str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    ) == PULL_PREFIX
