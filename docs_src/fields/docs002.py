from typing import Optional

import databases
import ormar
import sqlalchemy

database = databases.Database("sqlite:///db.sqlite")
metadata = sqlalchemy.MetaData()


class Department(ormar.Model):
    class Meta:
        database = database
        metadata = metadata

    id: int = ormar.Integer(primary_key=True)
    name: str = ormar.String(max_length=100)


class Course(ormar.Model):
    class Meta:
        database = database
        metadata = metadata

    id: int = ormar.Integer(primary_key=True)
    name: str = ormar.String(max_length=100)
    completed: bool = ormar.Boolean(default=False)
    department: Optional[Department] = ormar.ForeignKey(
        Department, related_name="my_courses"
    )


department = Department(name="Science")
course = Course(name="Math", completed=False, department=department)

print(department.my_courses[0])
# Will produce:
# Course(id=None,
#        name='Math',
#        completed=False,
#        department=Department(id=None, name='Science'))
