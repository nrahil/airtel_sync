"""Static Indian state reference tables.

Deliberately no pincode data: the dataset's pincodes are synthetic and do not
match states, so no rule may depend on them (edge case D13).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

# GSTN state codes (first two GSTIN digits).
GST_STATE_CODES: Final = MappingProxyType(
    {
        "01": "Jammu and Kashmir",
        "02": "Himachal Pradesh",
        "03": "Punjab",
        "04": "Chandigarh",
        "05": "Uttarakhand",
        "06": "Haryana",
        "07": "Delhi",
        "08": "Rajasthan",
        "09": "Uttar Pradesh",
        "10": "Bihar",
        "11": "Sikkim",
        "12": "Arunachal Pradesh",
        "13": "Nagaland",
        "14": "Manipur",
        "15": "Mizoram",
        "16": "Tripura",
        "17": "Meghalaya",
        "18": "Assam",
        "19": "West Bengal",
        "20": "Jharkhand",
        "21": "Odisha",
        "22": "Chhattisgarh",
        "23": "Madhya Pradesh",
        "24": "Gujarat",
        "26": "Dadra and Nagar Haveli and Daman and Diu",
        "27": "Maharashtra",
        "29": "Karnataka",
        "30": "Goa",
        "31": "Lakshadweep",
        "32": "Kerala",
        "33": "Tamil Nadu",
        "34": "Puducherry",
        "35": "Andaman and Nicobar Islands",
        "36": "Telangana",
        "37": "Andhra Pradesh",
        "38": "Ladakh",
    }
)

# Udyam registration state codes (``UDYAM-XX-..``). Only codes we can vouch for;
# an unknown code is reported by the audit as "unknown", never guessed.
UDYAM_STATE_CODES: Final = MappingProxyType(
    {
        "AP": "Andhra Pradesh",
        "AR": "Arunachal Pradesh",
        "AS": "Assam",
        "BR": "Bihar",
        "CH": "Chandigarh",
        "DL": "Delhi",
        "GA": "Goa",
        "GJ": "Gujarat",
        "HP": "Himachal Pradesh",
        "HR": "Haryana",
        "JH": "Jharkhand",
        "JK": "Jammu and Kashmir",
        "KL": "Kerala",
        "KR": "Karnataka",
        "MH": "Maharashtra",
        "ML": "Meghalaya",
        "MN": "Manipur",
        "MP": "Madhya Pradesh",
        "MZ": "Mizoram",
        "NL": "Nagaland",
        "OD": "Odisha",
        "PB": "Punjab",
        "PY": "Puducherry",
        "RJ": "Rajasthan",
        "SK": "Sikkim",
        "TN": "Tamil Nadu",
        "TR": "Tripura",
        "TS": "Telangana",
        "UK": "Uttarakhand",
        "UP": "Uttar Pradesh",
        "WB": "West Bengal",
    }
)

STATES_AND_UTS: Final = frozenset(GST_STATE_CODES.values())
