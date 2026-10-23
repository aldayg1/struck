"""Composable triggers (spec D4, §5), ported from 
``crypto/bots/triggers.py`` / ``trigger_engine.py`` .
A fired trigger only PROPOSES; every proposal needs a fresh human approval
unless the optional D5 policy covers it."""
