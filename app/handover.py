"""Legacy write-path ownership checks, including when pools are disabled."""
MARKER = ".pool-read-only"


class HandoverError(Exception):
    def __init__(self, account_id):
        self.account_id = account_id
        super().__init__(f"this login now belongs to pool account {account_id}; release it in Settings > Accounts first")


def owner(slot):
    try:
        return (slot / MARKER).read_text().strip()
    except FileNotFoundError:
        return None


def check_slot(slot):
    account_id = owner(slot)
    if account_id is not None:
        raise HandoverError(account_id)
