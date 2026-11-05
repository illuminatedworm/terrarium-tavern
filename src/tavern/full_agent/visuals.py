"""Frozen json-render specs, backed only by successful native tool exchanges.

This is a deliberately small component catalog, not arbitrary SVG or script input.
The same boundary validates model output and imported successful tool calls.
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tavern.full_agent.protocol import succeeded


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Coordinate = Annotated[float, Field(ge=0, le=100, description="Canvas percentage, 0–100 inclusive.")]
Paint = Literal["none", "surface", "muted", "ink", "accent", "secondary"]
StatePath = Literal["/selection", "/detail"]


class Binding(StrictModel):
    state: StatePath = Field(alias="$state")


TextValue = Annotated[str, Field(max_length=4000)] | Binding


class Shape(StrictModel):
    showFor: list[Annotated[str, Field(max_length=40)]] | None = Field(
        default=None, min_length=1, max_length=12
    )


class Rect(Shape):
    kind: Literal["rect"]
    x: Coordinate
    y: Coordinate
    width: Coordinate
    height: Coordinate
    fill: Paint
    stroke: Paint


class Ellipse(Shape):
    kind: Literal["ellipse"]
    x: Coordinate
    y: Coordinate
    rx: Coordinate
    ry: Coordinate
    fill: Paint
    stroke: Paint


class Line(Shape):
    kind: Literal["line"]
    x1: Coordinate
    y1: Coordinate
    x2: Coordinate
    y2: Coordinate
    stroke: Paint
    dashed: bool = False


class Path(Shape):
    kind: Literal["path"]
    points: list[Annotated[list[Coordinate], Field(min_length=2, max_length=2)]] = Field(
        min_length=2, max_length=30
    )
    closed: bool
    fill: Paint
    stroke: Paint
    dashed: bool = False


class Label(Shape):
    kind: Literal["label"]
    x: Coordinate
    y: Coordinate
    text: str = Field(min_length=1, max_length=200)
    anchor: Literal["start", "middle", "end"]


Primitive = Annotated[Rect | Ellipse | Line | Path | Label, Field(discriminator="kind")]


class SceneProps(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    height: int = Field(ge=240, le=600)
    selection: TextValue
    items: list[Primitive] = Field(min_length=1, max_length=100)


class StackProps(StrictModel):
    direction: Literal["vertical", "horizontal"]


class TextProps(StrictModel):
    text: TextValue


class ButtonProps(StrictModel):
    label: str = Field(min_length=1, max_length=60)


class StateParams(StrictModel):
    statePath: StatePath
    value: str = Field(max_length=4000)


class DraftParams(StrictModel):
    text: TextValue


class StateAction(StrictModel):
    action: Literal["setState"]
    params: StateParams


class DraftAction(StrictModel):
    action: Literal["composeDraft"]
    params: DraftParams


Action = Annotated[StateAction | DraftAction, Field(discriminator="action")]


class Events(StrictModel):
    press: Action | Annotated[list[Action], Field(min_length=1, max_length=3)]


class Element(StrictModel):
    children: list[str] = Field(max_length=40)


class StackElement(Element):
    type: Literal["Stack"]
    props: StackProps


class TextElement(Element):
    type: Literal["Text"]
    props: TextProps


class ButtonElement(Element):
    type: Literal["Button"]
    props: ButtonProps
    on: Events


class SceneElement(Element):
    type: Literal["Scene"]
    props: SceneProps


UIElement = Annotated[StackElement | TextElement | ButtonElement | SceneElement, Field(discriminator="type")]
ElementKey = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,30}$")]


class UIState(StrictModel):
    selection: str = Field(max_length=40)
    detail: str = Field(max_length=4000)


class VisualSpec(StrictModel):
    root: ElementKey
    elements: dict[ElementKey, UIElement] = Field(min_length=1, max_length=40)
    state: UIState

    @model_validator(mode="after")
    def tree(self):
        visited, pending, scenes = set(), [self.root], 0
        while pending:
            key = pending.pop()
            if key not in self.elements or key in visited:
                raise ValueError(
                    "root/children must form one tree without missing, repeated or cyclic references"
                )
            visited.add(key)
            element = self.elements[key]
            if element.type != "Stack" and element.children:
                raise ValueError("only Stack accepts children")
            pending.extend(element.children)
            scenes += element.type == "Scene"
            if element.type == "Button":
                actions = element.on.press if isinstance(element.on.press, list) else [element.on.press]
                for action in actions:
                    if (
                        action.action == "setState"
                        and action.params.statePath == "/selection"
                        and len(action.params.value) > 40
                    ):
                        raise ValueError("selection values must contain at most 40 characters")
        if visited != self.elements.keys() or not 1 <= scenes <= 3:
            raise ValueError("all elements must belong to root; a spec needs 1–3 Scenes")
        return self


class PresentUI(StrictModel):
    # An open object is portable across providers' native tool schema subsets.
    # The catalog description and the server's discriminated models are authoritative.
    spec: dict[str, Any] = Field(
        description="Complete json-render spec; follow the catalog in the tool description."
    )


def validate_spec(spec):
    try:
        VisualSpec.model_validate(spec)
    except ValidationError as error:
        # Do not echo whole invalid objects or program-owned identities into a model request.
        details = error.errors(include_input=False, include_url=False, include_context=False)
        issue = details[0]
        location = "/".join(str(part) for part in issue["loc"]) or "spec"
        raise ValueError(f"Invalid visual spec at {location}: {issue['msg']}") from None


def successful_specs(records):
    """Return specs in call order, only after their paired result confirms success."""
    calls, pending, accepted = [], {}, set()
    for record in records:
        for call in record.get("tool_calls") or []:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                continue
            # Relays may reuse native call IDs in later responses. Pair a result
            # with the most recent call, never let an earlier success bless a
            # later failed spec or overwrite an earlier accepted artifact.
            pending.pop(call.get("id"), None)
            if call["function"].get("name") == "present_ui":
                if not isinstance(call.get("id"), str) or not isinstance(
                    call["function"].get("arguments"), str
                ):
                    raise ValueError("交互图形工具记录格式无效")
                pending[call["id"]] = len(calls)
                calls.append(call["function"]["arguments"])
        if record.get("role") == "tool" and record.get("tool_call_id") in pending:
            index = pending.pop(record["tool_call_id"])
            try:
                result = json.loads(record["content"])
            except (ValueError, TypeError):
                # Older/imported traces can contain plain-text tool results;
                # without an explicit receipt they never create a visual.
                continue
            if succeeded(result):
                accepted.add(index)
    specs = []
    for key, args in enumerate(calls):
        if key in accepted:
            try:
                specs.append(PresentUI.model_validate_json(args).spec)
            except ValidationError:
                raise ValueError("成功的交互图形工具记录参数无效") from None
    return specs


def message_visuals(store, sid, message_ids):
    from tavern.storage.store import NotFound

    groups = {mid: [] for mid in message_ids}
    placeholders = ",".join("?" for _ in groups)
    with store.connection() as db:
        # A batch needs branch identity, not Store.session()'s complete message
        # bodies, snapshots, card and settings. Keep the head check and lookup in
        # one read transaction, including if the user switches reply versions.
        db.execute("BEGIN")
        session = db.execute("SELECT agent_mode,kemini,head FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None:
            raise NotFound("会话不存在")
        if session["agent_mode"] != "full_agent" or session["kemini"]:
            raise ValueError("交互图形仅适用于普通 Full Agent 会话")
        allowed = {
            row[0]
            for row in db.execute(
                "WITH RECURSIVE branch(id,parent,role) AS ("
                "SELECT id,parent,role FROM messages WHERE session=? AND id=? UNION "
                "SELECT m.id,m.parent,m.role FROM messages m JOIN branch b ON m.id=b.parent WHERE m.session=?"
                ") SELECT id FROM branch WHERE role='assistant' AND id IN (" + placeholders + ")",
                (sid, session["head"], sid, *groups),
            )
        }
        if set(groups) != allowed:
            raise NotFound("图形不属于当前可见的回复版本")
        # Only present_ui arguments cross the SQLite/Python boundary. Other calls
        # contribute their small identities solely to delimit reused call IDs;
        # tool results are reduced in SQL to the first matching success flag.
        rows = db.execute(
            "WITH calls AS MATERIALIZED ("
            "SELECT t.message_id,t.seq,CAST(c.key AS INTEGER) AS position,"
            "json_extract(c.value,'$.id') AS call_id,"
            "json_extract(c.value,'$.function.name') AS name,"
            "CASE WHEN json_extract(c.value,'$.function.name')='present_ui' "
            "THEN json_extract(c.value,'$.function.arguments') END AS arguments,"
            "lead(t.seq) OVER (PARTITION BY t.message_id,json_extract(c.value,'$.id') "
            "ORDER BY t.seq,CAST(c.key AS INTEGER)) AS next_call "
            "FROM full_agent_trace t,json_each(t.payload,'$.tool_calls') c "
            "WHERE t.session=? AND t.message_id IN (" + placeholders + ")"
            ") SELECT c.message_id,c.arguments FROM calls c WHERE c.name='present_ui' AND ("
            "SELECT CASE WHEN json_valid(json_extract(r.payload,'$.content')) "
            "THEN json_type(json_extract(r.payload,'$.content'),'$.ok') END "
            "FROM full_agent_trace r WHERE r.session=? AND r.message_id=c.message_id "
            "AND r.seq>c.seq AND (c.next_call IS NULL OR r.seq<c.next_call) "
            "AND json_extract(r.payload,'$.role')='tool' "
            "AND json_extract(r.payload,'$.tool_call_id')=c.call_id ORDER BY r.seq LIMIT 1"
            ")='true' ORDER BY c.seq,c.position",
            (sid, *groups, sid),
        )
        for row in rows:
            groups[row["message_id"]].append(json.loads(row["arguments"])["spec"])
    return groups


DESCRIPTION = """Show a frozen interactive graphic below this turn's story when it helps (map, relationship graph, structure diagram); do not repeat panels every turn. At most 3 accepted specs per turn; failed calls do not count. To change an older graphic, send a new complete spec in a later turn. No HTML, SVG markup, scripts or URLs.
Spec: {root:'root',elements:{root:{type:'Stack',props:{direction:'vertical'},children:['map','go']},map:{type:'Scene',props:{...},children:[]},go:{type:'Button',props:{label:'Go'},on:{press:{...}},children:[]}},state:{selection:'overview',detail:''}}. Every element has type, props and children; only Stack has children. Keys: a letter, then letters/digits/_/- (max 31). At most 40 elements forming one tree from root, with 1-3 Scenes. state has exactly selection (max 40 chars) and detail (max 4000).
Stack: direction 'vertical'|'horizontal'. Text: text (string or {$state:'/detail'}). Button: label (max 60); on.press sits beside props.
Scene: title (max 120), height (240-600 px), selection {$state:'/selection'}, items (1-100, drawn in order; put labels last). The canvas is 768 px wide: coordinates are 0-100 percent, x/width/rx of 768 and y/height/ry of height.
Items have kind and optional showFor (1-12 selection values; omit to always show): rect x,y,width,height,fill,stroke; ellipse x,y,rx,ry,fill,stroke; line x1,y1,x2,y2,stroke,dashed?; path points [[x,y],...] (2-30, straight segments),closed,fill,stroke,dashed?; label x,y,text (max 200),anchor 'start'|'middle'|'end'. Paint: none|surface|muted|ink|accent|secondary. Mark unconfirmed relations with dashed lines and labels; keep labels apart.
press is one action or a list of 1-3: setState {statePath:'/selection'|'/detail',value}, or composeDraft {text: string or {$state:'/detail'}}, which only appends to the user's input for them to edit and send; never imply it happened. Prose goes in submit_story; do not restate the spec."""
