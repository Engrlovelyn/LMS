"""Subject-specific content for seeding demo classrooms.

The generic demo seeders (``seed_sample_data``, ``seed_teacher_demo``,
``seed_demo_data``) give every classroom the same placeholders -- "Topic 1:
Foundations", a "Lesson Handout", a "Quiz 1" whose items all read "2 + 2 = ?".
Inside an individual *virtual classroom* those placeholders are correctly
scoped to the classroom's subject, but every subject ends up looking identical,
which is what makes the demo feel fake.

This module holds a small concept bank per subject. From those concepts the
``seed_subject_content`` command derives subject-specific topics, learning
materials and the five takeable assessments (multiple choice, identification,
true/false, short answer + essay, and a long quiz), so an English classroom and
a Chemistry classroom read completely differently.

Each subject maps to a dict with a ``concepts`` list of ``(term, definition)``
pairs. At least four concepts are needed so multiple-choice questions have
distractors; five or six keep the generated banks varied.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# The concept bank. Keyed by the exact ``Subject.name`` stored in the database.
# ---------------------------------------------------------------------------
SUBJECT_CONTENT = {
    "Applied Economics": {
        "concepts": [
            ("Scarcity", "the condition where resources are limited but wants are unlimited"),
            ("Opportunity cost", "the value of the next best alternative given up when making a choice"),
            ("Demand", "the quantity of a good buyers are willing and able to purchase at a given price"),
            ("Supply", "the quantity of a good producers are willing to sell at a given price"),
            ("Inflation", "a general rise in the prices of goods and services over time"),
            ("Gross domestic product", "the total value of goods and services produced within a country"),
        ],
    },
    "Araling Panlipunan": {
        "concepts": [
            ("Kasaysayan", "pag-aaral ng mga pangyayari sa nakaraan"),
            ("Heograpiya", "pag-aaral ng mga anyong lupa, tubig, at klima"),
            ("Kultura", "kaugalian, paniniwala, at pamumuhay ng isang pangkat"),
            ("Pamahalaan", "institusyong namamahala at naglilingkod sa mga mamamayan"),
            ("Mamamayan", "taong kasapi ng isang bansa na may karapatan at tungkulin"),
            ("Ekonomiya", "paraang ginagamit sa pagtugon sa pangangailangan ng lipunan"),
        ],
    },
    "Basic Calculus": {
        "concepts": [
            ("Limit", "the value a function approaches as the input approaches a given point"),
            ("Derivative", "the instantaneous rate of change of a function"),
            ("Integral", "the accumulation of quantities, often interpreted as area under a curve"),
            ("Continuity", "a property of a function with no breaks or holes at a point"),
            ("Slope of a tangent", "the derivative value describing the steepness at a point"),
            ("Differentiation", "the process of finding the derivative of a function"),
        ],
    },
    "Business Ethics and Social Responsibility": {
        "concepts": [
            ("Business ethics", "applying moral principles to business decisions and behavior"),
            ("Corporate social responsibility", "a company's duty to act for the benefit of society"),
            ("Stakeholder", "any person or group affected by a business's actions"),
            ("Integrity", "consistently doing what is right and honest in business"),
            ("Sustainability", "meeting present needs without harming future generations"),
            ("Accountability", "accepting responsibility for the results of one's actions"),
        ],
    },
    "Business Finance": {
        "concepts": [
            ("Capital", "the money and assets used to start and grow a business"),
            ("Liquidity", "how easily an asset can be converted into cash"),
            ("Budget", "a plan of expected income and expenses over a period"),
            ("Investment", "committing money with the expectation of earning a return"),
            ("Interest rate", "the percentage charged or earned on borrowed or invested money"),
            ("Financial statement", "a report summarizing a business's financial position"),
        ],
    },
    "Business Mathematics": {
        "concepts": [
            ("Markup", "the amount added to a product's cost to arrive at its selling price"),
            ("Markdown", "a reduction from the original selling price"),
            ("Discount", "an amount subtracted from a listed price"),
            ("Interest", "a fee paid for the use of money over time"),
            ("Principal", "the original amount of money borrowed or invested"),
            ("Profit", "the money left after costs are subtracted from revenue"),
        ],
    },
    "Christian Living": {
        "concepts": [
            ("Faith", "belief and trust in God"),
            ("Grace", "the free and undeserved gift of God's help"),
            ("Sacrament", "a visible sign instituted by Christ to give grace"),
            ("Prayer", "the raising of one's mind and heart to God"),
            ("Ten Commandments", "the moral law given by God to Moses"),
            ("Stewardship", "the responsible care of what God has entrusted to us"),
        ],
    },
    "Community Engagement": {
        "concepts": [
            ("Community", "a group of people living in the same place or sharing common interests"),
            ("Solidarity", "unity and mutual support among people"),
            ("Needs assessment", "a process of identifying a community's problems and resources"),
            ("Advocacy", "public support for a cause or policy"),
            ("Empowerment", "enabling people to take control of their own lives"),
            ("Participation", "involving community members in planning and decisions"),
        ],
    },
    "Computer Programming": {
        "concepts": [
            ("Variable", "a named storage location that holds a value"),
            ("Loop", "a control structure that repeats a block of code"),
            ("Function", "a reusable block of code that performs a task"),
            ("Conditional", "a statement that runs code only when a condition is true"),
            ("Array", "an ordered collection of values"),
            ("Algorithm", "a step-by-step procedure for solving a problem"),
        ],
    },
    "Creative Nonfiction": {
        "concepts": [
            ("Memoir", "a factual account of the author's own life experience"),
            ("Personal essay", "a short nonfiction piece based on the writer's thoughts and experience"),
            ("Point of view", "the perspective from which a narrative is told"),
            ("Imagery", "language that appeals to the senses"),
            ("Profile", "a factual article that portrays a real person"),
            ("Reflection", "the writer's thoughtful examination of experience"),
        ],
    },
    "Creative Writing": {
        "concepts": [
            ("Plot", "the sequence of events in a story"),
            ("Character", "a person or being in a literary work"),
            ("Setting", "the time and place where a story happens"),
            ("Metaphor", "a comparison that states one thing is another"),
            ("Conflict", "the central struggle between opposing forces"),
            ("Theme", "the underlying message or central idea of a work"),
        ],
    },
    "Disciplines and Ideas in the Social Sciences": {
        "concepts": [
            ("Sociology", "the study of society and social behavior"),
            ("Anthropology", "the study of human cultures and societies"),
            ("Psychology", "the study of the mind and behavior"),
            ("Economics", "the study of how people use scarce resources"),
            ("Political science", "the study of government and politics"),
            ("Historiography", "the study of how history is written"),
        ],
    },
    "EPP / TLE": {
        "concepts": [
            ("Entrepreneurship", "the activity of setting up and running a business"),
            ("Horticulture", "the science and art of growing plants"),
            ("Carpentry", "the craft of working with wood in construction"),
            ("Food preservation", "processing food to prevent spoilage"),
            ("Bookkeeping", "recording the financial transactions of a business"),
            ("Sewing", "joining or attaching materials with stitches"),
        ],
    },
    "Earth and Life Science": {
        "concepts": [
            ("Plate tectonics", "the theory explaining the movement of Earth's outer shell"),
            ("Photosynthesis", "the process by which plants make food using sunlight"),
            ("Ecosystem", "a community of organisms interacting with their environment"),
            ("Evolution", "the change in species over generations"),
            ("Rock cycle", "the continuous transformation of rock types"),
            ("Genetics", "the study of heredity and genes"),
        ],
    },
    "English": {
        "concepts": [
            ("Noun", "a word that names a person, place, thing, or idea"),
            ("Verb", "a word that expresses action or a state of being"),
            ("Adjective", "a word that describes or modifies a noun"),
            ("Simile", "a comparison using like or as"),
            ("Thesis statement", "the main claim of an essay"),
            ("Subject-verb agreement", "matching the verb with its subject in number"),
        ],
    },
    "English for Academic and Professional Purposes": {
        "concepts": [
            ("Abstract", "a concise summary of a research paper"),
            ("Citation", "a reference to a source of information"),
            ("Paraphrase", "restating a passage in one's own words"),
            ("Thesis statement", "the central argument of an academic text"),
            ("Literature review", "a survey of existing research on a topic"),
            ("Plagiarism", "using another's work without giving credit"),
        ],
    },
    "Entrepreneurship": {
        "concepts": [
            ("Business plan", "a written document describing a business and its goals"),
            ("Capital", "money used to start or grow a business"),
            ("Market research", "gathering information about customers and competitors"),
            ("Profit", "the money left after costs are subtracted from revenue"),
            ("Target market", "the specific group of customers a business serves"),
            ("Feasibility study", "an analysis of whether a project is practical"),
        ],
    },
    "Filipino": {
        "concepts": [
            ("Pandiwa", "salitang nagsasaad ng kilos o galaw"),
            ("Pangngalan", "salitang tumutukoy sa tao, bagay, lugar, o pangyayari"),
            ("Pang-uri", "salitang naglalarawan sa pangngalan"),
            ("Sanaysay", "maikling akdang nagpapahayag ng kaisipan at damdamin"),
            ("Tula", "akdang pampanitikan na may taludtod at sukat"),
            ("Balita", "ulat tungkol sa isang mahalagang pangyayari"),
        ],
    },
    "Fundamentals of Accountancy, Business and Management 2": {
        "concepts": [
            ("Asset", "a resource owned by a business that has economic value"),
            ("Liability", "a debt or obligation owed by a business"),
            ("Equity", "the owner's interest in a business"),
            ("Journal", "the book where transactions are first recorded"),
            ("Ledger", "the book that groups accounts together"),
            ("Balance sheet", "a statement of assets, liabilities, and equity"),
        ],
    },
    "General Chemistry 1": {
        "concepts": [
            ("Atom", "the smallest unit of an element"),
            ("Molecule", "a group of atoms bonded together"),
            ("Mole", "the amount of substance containing Avogadro's number of particles"),
            ("Ion", "an atom or molecule with an electric charge"),
            ("Covalent bond", "a bond formed by sharing electrons"),
            ("Stoichiometry", "the calculation of quantities in chemical reactions"),
        ],
    },
    "General Mathematics": {
        "concepts": [
            ("Function", "a relation where each input has exactly one output"),
            ("Domain", "the set of all possible inputs of a function"),
            ("Logarithm", "the exponent to which a base is raised to get a number"),
            ("Simple interest", "interest computed only on the original principal"),
            ("Rational function", "a ratio of two polynomial functions"),
            ("Inverse function", "a function that reverses the effect of another function"),
        ],
    },
    "General Physics 1": {
        "concepts": [
            ("Velocity", "the rate of change of displacement"),
            ("Acceleration", "the rate of change of velocity"),
            ("Force", "a push or pull that changes motion"),
            ("Momentum", "the product of an object's mass and velocity"),
            ("Energy", "the capacity to do work"),
            ("Newton's first law", "an object stays at rest or in motion unless acted on by a force"),
        ],
    },
    "Introduction to World Religions": {
        "concepts": [
            ("Monotheism", "the belief in one God"),
            ("Polytheism", "the belief in many gods"),
            ("Islam", "a religion founded on the teachings of Muhammad"),
            ("Buddhism", "a religion founded by Siddhartha Gautama"),
            ("Hinduism", "one of the oldest religions, originating in India"),
            ("Ritual", "a set of actions performed for religious purposes"),
        ],
    },
    "Introduction to the Philosophy of the Human Person": {
        "concepts": [
            ("Philosophy", "the love of wisdom and the study of fundamental questions"),
            ("Ethics", "the study of right and wrong conduct"),
            ("Logic", "the study of valid reasoning"),
            ("Metaphysics", "the study of reality and existence"),
            ("Epistemology", "the study of knowledge"),
            ("Free will", "the ability to choose one's own actions"),
        ],
    },
    "Komunikasyon at Pananaliksik": {
        "concepts": [
            ("Komunikasyon", "pagpapalitan ng impormasyon at kaisipan"),
            ("Wika", "sistemang ginagamit sa pakikipagtalastasan"),
            ("Pananaliksik", "maingat at sistematikong paghahanap ng impormasyon"),
            ("Tesis", "pangunahing pahayag sa isang pananaliksik"),
            ("Sanggunian", "pinagkunan ng impormasyon"),
            ("Balangkas", "planong gabay sa pagsulat"),
        ],
    },
    "MAPEH": {
        "concepts": [
            ("Rhythm", "the pattern of sounds and silences in music"),
            ("Melody", "a sequence of musical notes"),
            ("Harmony", "notes sounded together to produce chords"),
            ("Warm-up", "gentle exercise that prepares the body for activity"),
            ("Balance", "the ability to keep the body steady"),
            ("Composition", "the arrangement of elements in a work of art"),
        ],
    },
    "Makabansa": {
        "concepts": [
            ("Pagkakaisa", "pagsasama-sama tungo sa isang layunin"),
            ("Bayanihan", "pagkakaisa ng mga tao sa pagtulong sa kapwa"),
            ("Karapatan", "bagay na nararapat sa isang tao"),
            ("Tungkulin", "gawaing dapat gampanan ng isang mamamayan"),
            ("Pamahalaan", "institusyong namamahala sa bansa"),
            ("Simbolo ng bansa", "mga sagisag na kumakatawan sa Pilipinas"),
        ],
    },
    "Marketing": {
        "concepts": [
            ("Market segmentation", "dividing a market into distinct groups of buyers"),
            ("Product", "a good or service offered for sale"),
            ("Promotion", "activities that communicate and sell a product"),
            ("Pricing", "setting the amount charged for a product"),
            ("Distribution", "the process of getting a product to customers"),
            ("Customer", "a person who buys goods or services"),
        ],
    },
    "Mathematics": {
        "concepts": [
            ("Sum", "the result of addition"),
            ("Product", "the result of multiplication"),
            ("Fraction", "a number that represents part of a whole"),
            ("Equation", "a statement that two expressions are equal"),
            ("Perimeter", "the distance around a shape"),
            ("Area", "the amount of surface a shape covers"),
        ],
    },
    "Oral Communication": {
        "concepts": [
            ("Speaker", "the person who delivers a message"),
            ("Message", "the information being communicated"),
            ("Feedback", "the response of the receiver"),
            ("Channel", "the medium used to send a message"),
            ("Noise", "anything that interferes with communication"),
            ("Listening", "actively paying attention in order to understand a message"),
        ],
    },
    "Organization and Management": {
        "concepts": [
            ("Planning", "setting goals and deciding how to reach them"),
            ("Organizing", "arranging resources and tasks"),
            ("Leading", "guiding and motivating people"),
            ("Controlling", "monitoring and correcting performance"),
            ("Organization", "a group of people working toward a common goal"),
            ("Delegation", "assigning tasks and authority to others"),
        ],
    },
    "Pagbasa at Pagsusuri ng Iba't Ibang Teksto": {
        "concepts": [
            ("Pagsusuri", "masusing pag-aaral ng isang teksto"),
            ("Tema", "pangunahing kaisipan ng teksto"),
            ("Layunin", "dahilan kung bakit isinulat ang teksto"),
            ("Konklusyon", "pagtatapos o huling bahagi ng teksto"),
            ("Paglalahad", "paraang pagbibigay ng impormasyon"),
            ("Kritikal na pagbasa", "pagsusuri sa bisa at kahulugan ng teksto"),
        ],
    },
    "Personal Development": {
        "concepts": [
            ("Self-awareness", "knowing one's own thoughts and feelings"),
            ("Self-esteem", "how one values oneself"),
            ("Emotion", "a strong feeling such as joy or anger"),
            ("Goal setting", "deciding what one wants to achieve"),
            ("Resilience", "the ability to recover from difficulties"),
            ("Personality", "the pattern of thoughts and behaviors of a person"),
        ],
    },
    "Philippine Politics and Governance": {
        "concepts": [
            ("Democracy", "a system of government where power rests with the people"),
            ("Constitution", "the fundamental law of a nation"),
            ("Suffrage", "the right to vote"),
            ("Separation of powers", "dividing government among branches"),
            ("Sovereignty", "the supreme authority of a state"),
            ("Governance", "the way a state is ruled and managed"),
        ],
    },
    "Physical Education and Health": {
        "concepts": [
            ("Physical fitness", "the ability to perform daily tasks without undue fatigue"),
            ("Cardiovascular endurance", "the ability of the heart and lungs to supply oxygen"),
            ("Flexibility", "the range of motion of the joints"),
            ("Nutrition", "the process of taking in food for health and growth"),
            ("Warm-up", "gentle exercise done before an activity"),
            ("Healthy lifestyle", "habits that promote good health"),
        ],
    },
    "Physical Science": {
        "concepts": [
            ("Matter", "anything that has mass and takes up space"),
            ("Energy", "the capacity to do work"),
            ("Motion", "a change in position over time"),
            ("Force", "a push or pull on an object"),
            ("Atom", "the basic unit of matter"),
            ("Chemical change", "a change that forms one or more new substances"),
        ],
    },
    "Pre-Calculus": {
        "concepts": [
            ("Conic section", "a curve formed by the intersection of a plane and a cone"),
            ("Ellipse", "a closed curve where the sum of the distances to two foci is constant"),
            ("Parabola", "a curve where points are equidistant from a focus and a directrix"),
            ("Sequence", "an ordered list of numbers"),
            ("Series", "the sum of the terms of a sequence"),
            ("Trigonometry", "the study of angles and triangles"),
        ],
    },
    "Reading and Writing Skills": {
        "concepts": [
            ("Topic sentence", "the sentence that states the main idea of a paragraph"),
            ("Outline", "a plan that organizes ideas before writing"),
            ("Coherence", "the logical connection of ideas in a text"),
            ("Summary", "a brief statement of the main points"),
            ("Argument", "a claim supported by reasons and evidence"),
            ("Transition", "a word or phrase that links ideas"),
        ],
    },
    "Robotics": {
        "concepts": [
            ("Sensor", "a device that detects changes in the environment"),
            ("Actuator", "a component that causes movement"),
            ("Controller", "the part that processes input and controls output"),
            ("Algorithm", "a step-by-step set of instructions"),
            ("Automation", "operating with minimal human control"),
            ("Microcontroller", "a small computer on a single chip"),
        ],
    },
    "Science": {
        "concepts": [
            ("Photosynthesis", "the process by which plants make food using sunlight"),
            ("Matter", "anything that has mass and takes up space"),
            ("Ecosystem", "living things interacting with their environment"),
            ("Force", "a push or pull on an object"),
            ("Energy", "the capacity to do work"),
            ("Weather", "the condition of the atmosphere at a given time and place"),
        ],
    },
    "Statistics and Probability": {
        "concepts": [
            ("Mean", "the average of a set of numbers"),
            ("Median", "the middle value of an ordered set of numbers"),
            ("Mode", "the most frequent value in a set of numbers"),
            ("Probability", "the chance that an event will occur"),
            ("Sample", "a subset selected from a population"),
            ("Standard deviation", "a measure of how spread out the data are"),
        ],
    },
    "TLE (ICT)": {
        "concepts": [
            ("Hardware", "the physical parts of a computer"),
            ("Software", "programs that tell a computer what to do"),
            ("Network", "connected computers that share resources"),
            ("Database", "an organized collection of data"),
            ("Spreadsheet", "a program for organizing data in rows and columns"),
            ("Cybersecurity", "protecting systems and data from attacks"),
        ],
    },
    "Understanding Culture, Society and Politics": {
        "concepts": [
            ("Culture", "the beliefs, customs, and practices of a group"),
            ("Society", "a group of people living together in an organized way"),
            ("Socialization", "the process of learning culture and norms"),
            ("Institution", "an established pattern of social behavior"),
            ("Politics", "the activities associated with governing a society"),
            ("Norms", "shared rules of behavior in a society"),
        ],
    },
    "Values Education": {
        "concepts": [
            ("Respect", "showing regard for others and for oneself"),
            ("Honesty", "telling the truth and acting fairly"),
            ("Responsibility", "doing what one is expected to do"),
            ("Integrity", "doing the right thing even when no one is watching"),
            ("Empathy", "understanding and sharing the feelings of others"),
            ("Cooperation", "working together toward a common goal"),
        ],
    },
}

# A few subjects are stored under aliases; point them at the same content so the
# exact database name never has to be repeated above.
_ALIASES = {
    "Values Education (GMRC)": "Values Education",
    "ESP": "Values Education",
    "GMRC": "Values Education",
    "Edukasyon sa Pagpapakatao": "Values Education",
    "Technology and Livelihood Education": "TLE (ICT)",
    "TLE": "EPP / TLE",
    "Mathematics 1": "Mathematics",
    "Science 1": "Science",
    "English 1": "English",
    "Filipino 1": "Filipino",
}

_FALLBACK = {
    "concepts": [
        ("Core idea", "the central concept studied in this subject"),
        ("Key term", "an important word used repeatedly in this subject"),
        ("Principle", "a basic rule that explains how the subject works"),
        ("Process", "a series of steps used to complete a task in this subject"),
        ("Application", "the real-world use of what is learned in this subject"),
        ("Reflection", "thinking carefully about what has been learned"),
    ],
}


def normalize_subject(name):
    """Return a lookup key for ``name`` (trimmed, case-insensitive)."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def get_content(subject_name):
    """Return the content dict (concepts + derived topics/materials) for a subject."""
    raw = (subject_name or "").strip()
    content = SUBJECT_CONTENT.get(raw)
    if content is None:
        alias = _ALIASES.get(raw)
        if alias:
            content = SUBJECT_CONTENT.get(alias)
    if content is None:
        # Case/space-insensitive fallback lookup.
        wanted = normalize_subject(raw)
        for key, value in SUBJECT_CONTENT.items():
            if normalize_subject(key) == wanted:
                content = value
                break
    if content is None:
        content = _FALLBACK
    return {
        "subject": raw or "the subject",
        "concepts": list(content["concepts"]),
        "topics": _topics_for(raw, content["concepts"]),
        "materials": _materials_for(raw, content["concepts"]),
    }


def _topics_for(subject_name, concepts):
    """Derive 3-4 subject-specific topic names from the concept bank."""
    terms = [term for term, _ in concepts]
    topics = []
    for index, term in enumerate(terms[:4], start=1):
        topics.append("Unit {}: {}".format(index, term))
    if not topics:
        topics = ["Unit 1: {}".format(subject_name)]
    return topics


def _materials_for(subject_name, concepts):
    """Derive 4 subject-specific learning materials from the concept bank."""
    terms = [term for term, _ in concepts]
    subject_label = subject_name or "the subject"

    def term(index):
        return terms[index % len(terms)] if terms else subject_label

    return [
        ("{} Lesson Handout: {}".format(subject_label, term(0)), "document"),
        ("{} Practice Worksheet: {}".format(subject_label, term(1)), "pdf"),
        ("{} Presentation: {}".format(subject_label, term(2)), "presentation"),
        ("{} Study Guide".format(subject_label), "document"),
    ]
