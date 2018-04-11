from contentbase.upgrader import upgrade_step


@upgrade_step('provisionalClassification', '1', '2')
def provisionalClassification_1_2(value, system):
    # https://github.com/ClinGen/clincoded/issues/1196
    if 'replicatedOverTime' not in value:
        value['replicatedOverTime'] = False

    if 'contradictingEvidence' not in value:
        value['contradictingEvidence'] = {}
        value['contradictingEvidence']['proband'] = False
        value['contradictingEvidence']['caseControl'] = False
        value['contradictingEvidence']['experimental'] = False


@upgrade_step('provisionalClassification', '2', '3')
def provisionalClassification_2_3(value, system):
    # https://github.com/ClinGen/clincoded/issues/1414#issuecomment-335916100
    if 'alteredClassification' in value:
        if value['alteredClassification'] == 'No Selection':
            value['alteredClassification'] = 'No Modification'


@upgrade_step('provisionalClassification', '3', '4')
def provisionalClassification_3_4(value, system):
    # https://github.com/ClinGen/clincoded/issues/1507
    # Add affiliation property and update schema version
    return


@upgrade_step('provisionalClassification', '4', '5')
def provisionalClassification_4_5(value, system):
    # https://github.com/ClinGen/clincoded/issues/1417
    # Add various points properties and update schema version
    if 'totalScore' in value:
        value['classificationPoints']['evidencePointsTotal'] = value['totalScore']
        value.pop('totalScore', None)

    if 'classificationStatus' in value:
        if value['classificationStatus'] == 'Provisional':
            value['classificationStatus'] = 'In progress'
