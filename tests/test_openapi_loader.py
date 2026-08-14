from pathlib import Path

from thesis_rest_tester.loaders.openapi_loader import OpenAPILoader


def test_openapi_loader_extracts_operations(tmp_path: Path) -> None:
    spec = tmp_path / "openapi.yaml"
    spec.write_text(
        """
openapi: 3.0.3
security:
  - bearerAuth: []
paths:
  /proposals/{proposalId}:
    parameters:
      - in: path
        name: proposalId
        required: true
        schema: {type: integer}
    post:
      operationId: updateProposal
      summary: Update a proposal
      tags: [proposals]
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                title: {type: string}
      responses:
        "200": {description: Updated}
        "400": {description: Invalid input}
""",
        encoding="utf-8",
    )

    loaded = OpenAPILoader().load(spec)

    assert loaded.raw_document["openapi"] == "3.0.3"
    assert len(loaded.operations) == 1
    operation = loaded.operations[0]
    assert operation.method == "POST"
    assert operation.path == "/proposals/{proposalId}"
    assert operation.operation_id == "updateProposal"
    assert operation.parameters[0]["name"] == "proposalId"
    assert operation.request_body_schema == {
        "type": "object",
        "properties": {"title": {"type": "string"}},
    }
    assert operation.response_codes == ["200", "400"]
    assert operation.tags == ["proposals"]
    assert operation.auth_required is True



def _load_one(tmp_path: Path, document: str):
    spec = tmp_path / "openapi.yaml"
    spec.write_text(document, encoding="utf-8")
    return OpenAPILoader().load(spec).operations[0]


def test_a_referenced_body_arrives_with_its_fields(tmp_path: Path) -> None:
    """Most bodies in this corpus are named schemas, not written out in place.

    Returning the pointer left every downstream stage blind: nothing could read the
    fields, so the Test Writer invented them and the service rejected the result.
    """

    operation = _load_one(
        tmp_path,
        """
openapi: 3.0.3
paths:
  /citizen/signup:
    post:
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CitizenRegistrationRequest'
      responses: {'201': {description: created}}
components:
  schemas:
    CitizenRegistrationRequest:
      type: object
      required: [firstName, email]
      properties:
        firstName: {type: string}
        email: {type: string}
""",
    )
    assert operation.request_body_schema is not None
    assert operation.request_body_schema["required"] == ["firstName", "email"]
    assert set(operation.request_body_schema["properties"]) == {"firstName", "email"}


def test_a_pointer_that_leads_nowhere_is_left_visible(tmp_path: Path) -> None:
    """Seventeen of the eighteen specifications reference schemas they do not define.

    A dangling pointer is a fact about the specification, and the compaction step needs
    it to tell an undescribed body apart from one that takes no fields.
    """

    operation = _load_one(
        tmp_path,
        """
openapi: 3.0.3
paths:
  /session:
    post:
      requestBody:
        content:
          application/json:
            schema: {$ref: '#/components/schemas/LoginRequest'}
      responses: {'200': {description: ok}}
""",
    )
    assert operation.request_body_schema == {"$ref": "#/components/schemas/LoginRequest"}


def test_allOf_branches_are_flattened_into_one_set_of_fields(tmp_path: Path) -> None:
    """`allOf` is how these specifications express "the base request, plus a field"."""

    operation = _load_one(
        tmp_path,
        """
openapi: 3.0.3
paths:
  /reports:
    post:
      requestBody:
        content:
          application/json:
            schema:
              allOf:
                - $ref: '#/components/schemas/ReportBase'
                - type: object
                  required: [categoryId]
                  properties:
                    categoryId: {type: integer}
      responses: {'201': {description: created}}
components:
  schemas:
    ReportBase:
      type: object
      required: [title]
      properties:
        title: {type: string}
""",
    )
    schema = operation.request_body_schema
    assert schema is not None
    assert set(schema["properties"]) == {"title", "categoryId"}
    assert schema["required"] == ["title", "categoryId"]


def test_a_self_referential_schema_does_not_hang_the_loader(tmp_path: Path) -> None:
    operation = _load_one(
        tmp_path,
        """
openapi: 3.0.3
paths:
  /nodes:
    post:
      requestBody:
        content:
          application/json:
            schema: {$ref: '#/components/schemas/Node'}
      responses: {'201': {description: created}}
components:
  schemas:
    Node:
      type: object
      properties:
        name: {type: string}
        parent: {$ref: '#/components/schemas/Node'}
""",
    )
    assert operation.request_body_schema is not None
    assert "name" in operation.request_body_schema["properties"]


def test_a_swagger_2_body_parameter_is_resolved_too(tmp_path: Path) -> None:
    operation = _load_one(
        tmp_path,
        """
swagger: '2.0'
paths:
  /users:
    post:
      parameters:
        - in: body
          name: body
          schema: {$ref: '#/definitions/NewUser'}
      responses: {'201': {description: created}}
definitions:
  NewUser:
    type: object
    required: [username]
    properties:
      username: {type: string}
""",
    )
    assert operation.request_body_schema is not None
    assert operation.request_body_schema["required"] == ["username"]
