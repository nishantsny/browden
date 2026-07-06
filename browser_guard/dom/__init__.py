# SPDX-FileCopyrightText: 2026 Nishant
# SPDX-License-Identifier: Apache-2.0

"""Pure DOM helpers: query primitives and element serialization (no Selenium)."""
from . import query, serialize
from ._helpers import tag_class_list

__all__ = ["query", "serialize", "tag_class_list"]
