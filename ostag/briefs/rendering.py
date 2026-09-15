"""A05 local, separately authorized preview, never the frozen U01 TEXT delivery.

Composition supplies stored validated revisions and trusted connector SourceLinks.
No model object is interpreted as a card, callback or URL. Visible defanging maps
ALL punctuation/symbol/control characters in untrusted strings to a displayed
Unicode code point (e.g. dot becomes ⟦U+002E⟧). Letters/numbers/spaces alone survive;
this deliberately sacrifices punctuation fidelity instead of trusting plain_text
client autolink behavior. Trusted SourceLink button URLs are the sole exception.

Each page retains the deterministic review/coverage notice and gap count. Long
strings are split visibly into numbered parts, including EVERY coverage gap.
Pages are explicitly selected locally or by separately issued bound callbacks;
card_delivery owns the separate opt-in card-v1 outbox and delivery lifecycle.
This pure renderer provides no scheduler, rollout or device proof.
An oversized trusted URL is visibly omitted, never shortened to another URL;
the source identifiers and review/coverage content remain available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import unicodedata

from ostag.briefs.delivery import FeishuBinding
from ostag.briefs.models import SourceSet, validate_claims
from ostag.briefs.store import OutboxSnapshot, PrivateDestination, RequesterContext
from ostag.briefs.validation import (
    SECTIONS, VALIDATION_VERSION, Coverage, ValidatedDraft, source_links, strict_json,
    validate_output,
)

MAX_CARD_BYTES = 16384
_NOTICE = ('Model claims are not verified; semantic review required. '
           'Commitments are source-reported; proposals and decisions await human review. '
           'A preview is not approval or permission to act.')
_STATES = {'queued': 'queued', 'collecting': 'collecting', 'generating': 'generating',
           'draft_ready': 'draft ready', 'needs_input': 'input needed', 'failed': 'failed',
           'cancelled': 'cancelled', 'pending': 'pending', 'started': 'pending verification',
           'unknown': 'pending verification', 'provider_accepted': 'provider accepted'}
_TOKEN = re.compile(r'br_[A-Za-z0-9_-]{22,64}\Z')


class RenderError(ValueError):
    """Fixed private-preview diagnostic."""


def defang(value: str) -> str:
    if type(value) is not str or len(value) > 1000000:
        raise RenderError('invalid preview')
    return ''.join(c if c == ' ' or unicodedata.category(c)[0] in ('L', 'N')
                   else f'⟦U+{ord(c):04X}⟧' for c in value)


def _plain(text):
    return {'tag': 'plain_text', 'content': text}


def _element(text):
    return {'tag': 'div', 'text': _plain(text)}


def _bytes(card):
    # ASCII serialization is a conservative upper bound on UTF-8 JSON variants.
    return len(json.dumps(card, ensure_ascii=True).encode('utf-8'))


@dataclass(frozen=True)
class PrivatePreview:
    """Trusted detached snapshot; authorization is mandatory in the controller.

    Low-level render_private is pure and assumes its trusted caller authorized
    this exact snapshot. HTTP users must go through BriefCallbackController.
    """
    scope: RequesterContext
    job_id: str
    revision_id: str
    destination: PrivateDestination
    binding: FeishuBinding
    sources: SourceSet = field(repr=False)
    draft: ValidatedDraft = field(repr=False)
    state: str = 'draft_ready'
    outbox_id: str | None = None

    def __post_init__(self):
        if (type(self.scope) is not RequesterContext or type(self.binding) is not FeishuBinding
                or type(self.destination) is not PrivateDestination
                or type(self.sources) is not SourceSet or type(self.draft) is not ValidatedDraft
                or self.scope.principal.platform != 'feishu'
                or self.destination.principal != self.scope.principal
                or self.scope.namespace != self.sources.namespace
                or self.scope.principal not in self.sources.allowed_audience
                or any(self.scope.principal not in s.allowed_audience for s in self.sources.sources)
                or not re.fullmatch(r'oc_[A-Za-z0-9_-]{1,250}', self.destination.address)
                or any(type(v) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', v)
                       for v in (self.job_id, self.revision_id))
                or self.state not in _STATES):
            raise RenderError('invalid preview')
        validate_claims(self.sources, tuple(item.claim for section in SECTIONS
                                          for item in getattr(self.draft, section)))
        source_links(self.sources, self.draft.links)
        if _bytes(self.draft.to_dict()) > 6 * 1048576:
            raise RenderError('invalid preview')

    @classmethod
    def from_outbox(cls, scope, outbox: OutboxSnapshot, binding, sources: SourceSet,
                    *, links=(), state='draft_ready'):
        """Decode ONLY a trusted stored A03 revision; links come from fresh mapping.

        The mapping is crosschecked against both frozen source IDs and the stored
        validated link list. Reauthorizing sources/grants is the controller's job.
        """
        result = None
        try:
            if (type(outbox) is not OutboxSnapshot
                    or outbox.revision.validation_version != VALIDATION_VERSION):
                raise RenderError('invalid preview')
            payload = strict_json(outbox.revision.payload_json, 1048576)
            provenance = strict_json(outbox.revision.provenance_json, 1048576)
            if provenance.get('sources') != sources.to_dict():
                raise RenderError('invalid preview')
            expected = {*SECTIONS, 'coverage', 'status', 'notice', 'links', 'validation_version'}
            if set(payload) != expected or payload['validation_version'] != VALIDATION_VERSION:
                raise RenderError('invalid preview')
            trusted = source_links(sources, links)
            if payload['links'] != [dict(source_id=p.source_id, url=p.url) for p in trusted]:
                raise RenderError('invalid preview')
            draft = validate_output(json.dumps({'version': 'brief-output-v1'} |
                {s: payload[s] for s in SECTIONS}), sources=sources,
                coverage=Coverage.from_dict(payload['coverage']), max_output_bytes=1048576,
                links=trusted)
            if draft.to_dict() != payload:
                raise RenderError('invalid preview')
            result = cls(scope, outbox.revision.job_id, outbox.revision.revision_id,
                         outbox.destination, binding, sources, draft, state, outbox.delivery_id)
        except Exception:
            pass
        if result is None:
            raise RenderError('invalid stored preview')
        return result


def _parts(text):
    # Split the original characters before defanging: visible escapes stay whole.
    parts, current, size = [], [], 0
    for char in text:
        safe = defang(char)
        cost = len(json.dumps(safe, ensure_ascii=True))
        if current and size + cost > 1500:
            parts.append(''.join(current))
            current, size = [], 0
        current.append(safe)
        size += cost
    if current:
        parts.append(''.join(current))
    return parts or ['(empty)']


def _units(preview):
    draft = preview.draft
    units = []
    def prose(label, text):
        chunks = _parts(text)
        for n, chunk in enumerate(chunks, 1):
            suffix = f' — part {n}/{len(chunks)}' if len(chunks) > 1 else ''
            units.append(_element(label + suffix + ': ' + chunk))
    for i, gap in enumerate(draft.coverage.gaps, 1):
        prose(f'Coverage gap {i}', gap)
    for section in SECTIONS:
        for item in getattr(draft, section):
            kind = {'fact': 'Unverified fact', 'commitment': 'Source-reported commitment',
                    'proposal': 'Proposal awaiting review'}[item.claim.kind]
            prose(section.capitalize() + ' / ' + kind, item.claim.text)
            prose('Claim source IDs', ' '.join(item.claim.source_ids))
    links = {link.source_id: link.url for link in source_links(preview.sources, draft.links)}
    for source in preview.sources.sources:
        prose('Source ID / resource / version',
              f'{source.source_id} / {source.resource_id} / {source.resource_version}')
        if source.source_id in links:
            button = {'tag': 'action', 'actions': [{
                'tag': 'button', 'text': _plain('Open trusted source'),
                'url': links[source.source_id], 'type': 'default'}]}
            units.append(button if _bytes(button) <= 5000 else _element(
                'Trusted source link omitted: exceeds card size limit. Use the source ID.'))
    return units


def render_private(preview: PrivatePreview, *, page: int = 0, callback_id: str | None = None):
    """Render one bounded page; callback value is EXACTLY an opaque callback_id."""
    if type(preview) is not PrivatePreview or type(page) is not int or page < 0:
        raise RenderError('invalid preview')
    if callback_id is not None and (type(callback_id) is not str or not _TOKEN.fullmatch(callback_id)):
        raise RenderError('invalid callback')
    units = _units(preview)
    pages, current, size = [], [], 0
    for unit in units:
        cost = _bytes(unit)
        if current and (size + cost > 11000 or len(current) == 7):
            pages.append(current)
            current, size = [], 0
        current.append(unit)
        size += cost
    if current or not pages:
        pages.append(current)
    if page >= len(pages):
        raise RenderError('invalid page')
    coverage = ('Collection reports complete coverage.' if preview.draft.coverage.complete else
        f'Partial collection: {len(preview.draft.coverage.gaps)} coverage gaps. '
        'All gaps appear first across these numbered pages; review every page.')
    elements = [_element('Task ' + defang(preview.job_id) + ' / revision ' +
                         defang(preview.revision_id) + ': ' + _STATES[preview.state]),
                _element(coverage + ' ' + _NOTICE),
                _element(f'Page {page+1} of {len(pages)}')] + pages[page]
    elements.append(_element('Next step: review all coverage gaps and cited sources; '
                             'request another preview page through the authorized controller.'))
    if callback_id is not None:
        elements.append({'tag': 'action', 'actions': [{'tag': 'button',
            'text': _plain('Refresh preview'), 'value': {'callback_id': callback_id}}]})
    card = {'config': {'wide_screen_mode': False}, 'elements': elements}
    if len(elements) > 12 or _bytes(card) > MAX_CARD_BYTES:
        raise RenderError('preview page exceeds bound')
    return card


def render_group_status(job_id: str, state: str):
    """Accept no private snapshot, title, error, source, or receipt argument."""
    if type(job_id) is not str or len(job_id) > 128 or state not in _STATES:
        raise RenderError('invalid status')
    return {'elements': [_element('Task ' + defang(job_id)), _element(_STATES[state])]}


@dataclass(frozen=True)
class NavigationPage:
    """Bounded page metadata; private card stays out of repr."""
    page: int
    pages: int
    card: dict = field(repr=False)


def render_navigation(preview: PrivatePreview, *, page=0, controls=None):
    """card-v1 controls, preserving the legacy renderer and its fail-closed bounds.

    The existing renderer determines pagination and defanging. Navigation replaces
    its final local instruction with bounded opaque controls; URLs remain solely
    trusted SourceLinks. No legacy br_ token is accepted here.
    """
    card = render_private(preview, page=page)
    count = int(re.fullmatch(r'Page [0-9]+ of ([0-9]+)',
                           card['elements'][2]['text']['content'])[1])
    if count > 65536:
        raise RenderError('invalid page')
    expected = {'Refresh'} | ({'Previous'} if page else set()) | (
        {'Next'} if page + 1 < count else set())
    if controls is not None:
        if (type(controls) is not dict or set(controls) != expected
                or any(type(t) is not str or not re.fullmatch(r'bc_[A-Za-z0-9_-]{32}', t)
                       for t in controls.values())):
            raise RenderError('invalid callback')
        card['elements'][-1] = {'tag': 'action', 'actions': [
            {'tag': 'button', 'text': _plain(label), 'value': {'callback_id': controls[label]}}
            for label in ('Previous', 'Refresh', 'Next') if label in controls]}
    if _bytes(card) > MAX_CARD_BYTES or len(card['elements']) > 12:
        raise RenderError('preview page exceeds bound')
    return NavigationPage(page, count, card)
