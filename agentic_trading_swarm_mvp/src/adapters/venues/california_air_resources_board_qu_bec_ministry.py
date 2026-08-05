"""Compatibility import for the CARB/Quebec public-auction adapter.

The generated adapter-spec slug represents the accented name as ``qu_bec``.
Keep that import path available while the implementation uses an ASCII module
name that remains easy to reference from configuration and documentation.
"""

from adapters.venues.california_air_resources_board_quebec_ministry import (
    CaliforniaAirResourcesBoardQuebecMinistryAdapter,
    CaliforniaQuebecAuctionParseError,
    MARKET_SURFACE,
    NOTICE_REPORTS_URL,
    PRINTABLE_RESULT_URL,
    SOURCE_URL,
    SUMMARY_RESULTS_URL,
    parse_california_quebec_joint_auction,
    parse_carb_quebec_auction,
    parse_carb_quebec_joint_auction,
)

