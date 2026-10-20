"""Errors the HTTP layer maps to user-facing statuses."""


class NotFound(KeyError):
    """A requested record does not exist; the API answers 404 with this message.

    Plain KeyError stays a programming error (500) instead of masquerading as 404.
    """

    def __str__(self):
        # KeyError.__str__ quotes its argument; this message is shown verbatim.
        return str(self.args[0]) if self.args else ""
