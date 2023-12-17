# type: ignore
import databases
import pytest
import sqlalchemy
from pydantic import PydanticUserError, computed_field

import ormar
from ormar import ModelDefinitionError
from tests.settings import DATABASE_URL

database = databases.Database(DATABASE_URL, force_rollback=True)
metadata = sqlalchemy.MetaData()


class Song(ormar.Model):
    ormar_config = ormar.OrmarConfig(
        tablename="songs",
        metadata=metadata,
        database=database,
    )

    id: int = ormar.Integer(primary_key=True)
    name: str = ormar.String(max_length=100)
    sort_order: int = ormar.Integer()

    @computed_field
    def sorted_name(self) -> str:
        return f"{self.sort_order}: {self.name}"

    @computed_field
    def sample(self) -> str:
        return "sample"

    @computed_field
    def sample2(self) -> str:
        return "sample2"


@pytest.fixture(autouse=True, scope="module")
def create_test_database():
    engine = sqlalchemy.create_engine(DATABASE_URL)
    metadata.drop_all(engine)
    metadata.create_all(engine)
    yield
    metadata.drop_all(engine)


@pytest.mark.asyncio
async def test_sort_order_on_main_model():
    async with database:
        await Song.objects.create(name="Song 3", sort_order=3)
        await Song.objects.create(name="Song 1", sort_order=1)
        await Song.objects.create(name="Song 2", sort_order=2)

        songs = await Song.objects.all()
        song_dict = [song.dict() for song in songs]
        assert all("sorted_name" in x for x in song_dict)
        assert all(
            x["sorted_name"] == f"{x['sort_order']}: {x['name']}" for x in song_dict
        )
        song_json = [song.json() for song in songs]
        assert all("sorted_name" in x for x in song_json)

        check_include = songs[0].dict(include={"sample"})
        assert "sample" in check_include
        assert "sample2" not in check_include
        assert "sorted_name" not in check_include

        check_include = songs[0].dict(exclude={"sample"})
        assert "sample" not in check_include
        assert "sample2" in check_include
        assert "sorted_name" in check_include


def test_wrong_definition():
    with pytest.raises(PydanticUserError):

        class WrongModel(ormar.Model):  # pragma: no cover
            @computed_field
            def test(self, aa=10, bb=30):
                pass
