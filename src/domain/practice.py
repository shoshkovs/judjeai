"""Which knowledge-base documents are shared practice and which stay private."""

# Documents saved from a user's own case (analysis with "save to knowledge base") belong to
# that case: they never become precedent cards, search results or entries of the shared list.
PRIVATE_SOURCE_TYPES = ("chat",)

# Status of a knowledge-base document that was read and gives no precedent (an annulled
# sentence, an acquittal); the indexer does not read it again.
NOT_PRACTICE = "not_practice"
