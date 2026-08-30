"""Self-check for the two fidelity guards: the Whisper loop stripper and the
Flow cleanup plausibility gate. Run: python test_fidelity.py"""
import dial_app as d

# ---- _strip_loop: kill decoder loops, leave real speech alone
assert d._strip_loop("call me tomorrow tomorrow tomorrow tomorrow tomorrow") \
    == "call me tomorrow"
assert d._strip_loop("thanks. thanks. thanks. thanks.") == "thanks."
assert d._strip_loop("see you later see you later see you later see you "
                     "later") == "see you later"
assert d._strip_loop("that is very very good") == "that is very very good"
assert d._strip_loop("no no no") == "no no no"          # under the threshold
assert d._strip_loop("") == ""

# ---- _plausible: the cut this whole change exists to stop
orig = ("so the deployment needs the Railway builder pinned to Nixpacks, and "
        "Sam has to confirm the ClearPath number before Thursday, and also "
        "remind me the invoice for Parou is still unpaid at 4200 dollars.")

polished = ("The deployment needs the Railway builder pinned to Nixpacks. Sam "
            "has to confirm the ClearPath number before Thursday. Also, "
            "remind me the invoice for Parou is still unpaid at 4200 dollars.")
ok, why = d.Engine._plausible(orig, polished)
assert ok, why

# half the points dropped - the exact failure. Padded to pass the old 0.55
# length floor, so only the content-word check can catch it.
cut = ("The deployment needs the Railway builder pinned to Nixpacks, which "
       "is an important configuration detail to get right before shipping "
       "anything at all to production this week.")
ok, why = d.Engine._plausible(orig, cut)
assert not ok and "content words" in why, (ok, why)

assert not d.Engine._plausible(orig, "")[0]
assert not d.Engine._plausible(orig, "a a a a a a a a a a a a a a a")[0]

# filler-only removal must NOT read as lost content, even though it takes the
# length well down - this is the case a high length floor wrongly rejected
ok, why = d.Engine._plausible(
    "so basically yeah I really just think actually you know we should ship "
    "the dialer worker tomorrow morning I mean",
    "I think we should ship the dialer worker tomorrow morning")
assert ok, why

print("ok")
