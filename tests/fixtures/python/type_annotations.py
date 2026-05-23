from typing import Optional, Union


class Dog:
    def speak(self) -> str:
        return "woof"

    def fetch(self, item: str) -> str:
        return item


class Cat:
    def speak(self) -> str:
        return "meow"


def create_dog() -> Dog:
    return Dog()


def process(animal: Dog, name: str) -> str:
    result = animal.speak()
    return result


def use_annotations():
    pet: Dog = Dog()
    pet.speak()

    other: Cat = Cat()
    other.speak()

    inferred = create_dog()
    inferred.fetch("ball")

    count: int = 0
    items: list[str] = []

    # Explicit annotation should win over constructor type
    base_animal: Dog = Cat()
    base_animal.speak()  # should resolve to Dog.speak, not Cat.speak


def optional_bracket(animal: Optional[Dog]) -> None:
    animal.speak()  # should resolve to Dog.speak


def optional_pipe(animal: Dog | None) -> None:
    animal.speak()  # should resolve to Dog.speak


def union_with_none(animal: Union[Dog, None]) -> None:
    animal.speak()  # should resolve to Dog.speak


def union_two_types(animal: Union[Dog, Cat]) -> None:
    animal.speak()  # should resolve to Dog.speak (first match)
