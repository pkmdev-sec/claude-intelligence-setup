"""Shared configuration for Acontext hook system."""

# Model used for all hybrid regex+LLM operations in hooks
# Change this single value to update all Haiku calls
HAIKU_MODEL = "claude-haiku-4-5"

# Confidence thresholds for classification and detection
TIER_CLASSIFIER_LLM_CONFIDENCE = 0.85  # Confidence assigned to LLM-classified messages
DYNAMICS_LLM_MIN_CONFIDENCE = 0.65     # Minimum confidence for Haiku-detected dynamics
DYNAMICS_REGEX_MIN_CONFIDENCE = 0.70   # Minimum confidence for regex-detected dynamics
CONTRADICTION_MIN_CONFIDENCE = 0.50    # Minimum confidence to report contradiction
CONTRADICTION_LLM_MAX_CONFIDENCE = 0.85  # Cap for LLM contradiction confidence

# Performance limits
MAX_CONTEXT_LINES = 20                 # Max context lines for dynamics analysis
MAX_FILE_PATH_LENGTH = 4096            # Max file path length for behavioral store
PRAXIS_SEARCH_TIMEOUT = 8             # Seconds to wait for learn CLI

# Consolidated analyzer settings
ANALYZER_MAX_TOKENS = 250              # Max tokens for consolidated Haiku response
ANALYZER_TIMEOUT = 5                   # Seconds before falling back to regex
ANALYZER_MIN_PROMPT_LENGTH = 15        # Skip analysis for very short messages
VALID_DYNAMIC_TYPES = {"redirect", "approval", "correction", "rejection", "preference"}
VALID_CONTRADICTION_TYPES = {"replacement", "negation", "reversal"}
VALID_WORKFLOW_TYPES = {"bug", "substantial", "medium", "trivial", "question"}
VALID_TIER_CATEGORIES = {
    "decision", "preference", "constraint", "negative_knowledge",
    "requirement", "design", "technology", "configuration",
    "bug", "review", "file_reference",
    "acknowledgment", "short_message", "hook_marker", "navigation", "tool_output",
    "general",
}
