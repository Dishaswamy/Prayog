"""Knowledge base: reference literature the Steward can cite in S3 and S4 (ADR-S10).

Integrated from the team's Scientific-RAG-System notebooks: PDF extraction and cleaning,
word-window chunking, ``bge`` embeddings and top-k retrieval. Storage is PostgreSQL (one
database, ADR-S09) and generation is the Steward's own Claude adapter, behind the same
sanitiser, citation validation and audit as every other context.
"""
