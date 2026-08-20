## Appendix A - Severity Levels

Severity one means customer-visible loss of a core capability with no
workaround available to the customer or to the responder. It pages immediately,
at any hour of any day, and the group lead is informed whether or not the page
has been acknowledged. A severity one incident always produces a written review
within five working days, and the review is published to the whole engineering
organisation rather than kept inside the group.

Severity two means a core capability is degraded but usable, or that a non-core
capability is entirely unavailable, and that a workaround exists and has been
written down somewhere the responder can find it. It pages during working hours
and queues quietly outside them. A written review is produced only when the
same condition has fired twice within a single quarter, on the reasoning that
one occurrence is an incident and two is a pattern.

Severity three covers everything visible to the group but not to a customer: a
saturated queue that is draining on its own, a read replica lagging inside its
stated tolerance, a certificate approaching expiry with several weeks in hand.
Severity three never pages anyone. It is triaged at the daily handover and then
either fixed, scheduled with an owner and a date, or explicitly accepted with
an expiry attached, because an accepted risk without an expiry is indis-
tinguishable from a forgotten one.

Severity is assigned by the responder and not by the alerting rule. An alert may
suggest a severity, and the responder may raise it freely, but lowering a
severity one requires a second engineer to agree in writing before the change
takes effect. The reasoning is asymmetric on purpose: over-reacting to a page
costs an hour of one engineer's evening, and under-reacting to one costs a
customer their afternoon.

Severity is also reassessed as an incident develops. A page that opens at
severity two and turns out to have a wider blast radius is raised in place,
keeping the original incident record, rather than closed and reopened. The
history of how a severity moved is part of what the written review examines,
because a severity that took two hours to reach its final value usually points
at a missing signal rather than a slow responder.

Every severity has a named owner for the duration of the incident, and the
owner is a person rather than a rotation. The owner may delegate any part of
the work but keeps the decision about when the incident ends. Handing the owner
role over mid-incident is allowed and is written into the record with a
timestamp, because an incident with two people quietly believing the other is
driving is the failure mode this rule exists to prevent.

An incident is closed when the customer-visible symptom is gone and the
responder can say, in one sentence, what caused it. If the symptom is gone but
the cause is unknown, the incident is lowered to severity three and stays open
with an expiry rather than being closed optimistically. Closing on the
disappearance of a symptom is how the same incident gets discovered twice, at
which point the second responder has none of the first one's context.

Reviews are blameless in the specific sense that they ask what made the wrong
action look reasonable at the time, and they are not blameless in the sense of
declining to name what broke. A review that cannot name a mechanism has not
finished. The group keeps its reviews in one place and reads them at the start
of each quarter, which is the only part of this process that has ever been
skipped for more than one quarter running.

## Handover

The outgoing on-call writes a handover note covering open incidents, anything
accepted with an expiry date, and any alert that fired more than twice during
the shift. The incoming on-call confirms receipt before the rotation flips, and
an unconfirmed handover keeps the outgoing engineer on the hook.

## Appendix A - Severity Levels

Severity one means customer-visible loss of a core capability with no
workaround available to the customer or to the responder. It pages immediately,
at any hour of any day, and the group lead is informed whether or not the page
has been acknowledged. A severity one incident always produces a written review
within five working days, and the review is published to the whole engineering
organisation rather than kept inside the group.

Severity two means a core capability is degraded but usable, or that a non-core
capability is entirely unavailable, and that a workaround exists and has been
written down somewhere the responder can find it. It pages during working hours
and queues quietly outside them. A written review is produced only when the
same condition has fired twice within a single quarter, on the reasoning that
one occurrence is an incident and two is a pattern.

Severity three covers everything visible to the group but not to a customer: a
saturated queue that is draining on its own, a read replica lagging inside its
stated tolerance, a certificate approaching expiry with several weeks in hand.
Severity three never pages anyone. It is triaged at the daily handover and then
either fixed, scheduled with an owner and a date, or explicitly accepted with
an expiry attached, because an accepted risk without an expiry is indis-
tinguishable from a forgotten one.

Severity is assigned by the responder and not by the alerting rule. An alert may
suggest a severity, and the responder may raise it freely, but lowering a
severity one requires a second engineer to agree in writing before the change
takes effect. The reasoning is asymmetric on purpose: over-reacting to a page
costs an hour of one engineer's evening, and under-reacting to one costs a
customer their afternoon.

Severity is also reassessed as an incident develops. A page that opens at
severity two and turns out to have a wider blast radius is raised in place,
keeping the original incident record, rather than closed and reopened. The
history of how a severity moved is part of what the written review examines,
because a severity that took two hours to reach its final value usually points
at a missing signal rather than a slow responder.

Every severity has a named owner for the duration of the incident, and the
owner is a person rather than a rotation. The owner may delegate any part of
the work but keeps the decision about when the incident ends. Handing the owner
role over mid-incident is allowed and is written into the record with a
timestamp, because an incident with two people quietly believing the other is
driving is the failure mode this rule exists to prevent.

An incident is closed when the customer-visible symptom is gone and the
responder can say, in one sentence, what caused it. If the symptom is gone but
the cause is unknown, the incident is lowered to severity three and stays open
with an expiry rather than being closed optimistically. Closing on the
disappearance of a symptom is how the same incident gets discovered twice, at
which point the second responder has none of the first one's context.

Reviews are blameless in the specific sense that they ask what made the wrong
action look reasonable at the time, and they are not blameless in the sense of
declining to name what broke. A review that cannot name a mechanism has not
finished. The group keeps its reviews in one place and reads them at the start
of each quarter, which is the only part of this process that has ever been
skipped for more than one quarter running.
