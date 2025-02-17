"""
    Analyze the data stored in the database, after the download and extraction processes have finished.
"""
import pandas as pd
import numpy as np
from datetime import datetime as dt, timedelta as td

from airflow.operators.python import PythonOperator
from airflow.utils.task_group import TaskGroup

from AuxiliaryFunctions import MongoDatabase


class DailyCOVIDData:
    """
        Daily data of the COVID pandemic in Spain, with the number of new cases, hospitalizations, and deaths by
        Autonomous Region and age range.
    """

    @staticmethod
    def calculate_increase_percentage(data):
        """Return the percentage increase or decrease in the new cases, deaths, or hospitalizations"""
        if data[0] == 0:
            return 0
        return 100 * ((data[-1] - data[0]) / data[0])

    def __init__(self):
        """Load the data from the database and store it into a Pandas DataFrame"""
        # Connection to the extracted data database for reading, and to the analyzed data for writing
        self.db_read = MongoDatabase(MongoDatabase.extracted_db_name)
        self.db_write = MongoDatabase(MongoDatabase.analyzed_db_name)

        # Load the data from the DB
        self.df = self.db_read.read_data('daily_data')
        self.population_df = self.db_read.read_data('population_ar')

        # Aggregate the data
        self.__merge__population__()

    def __merge__population__(self):
        """Merge the COVID daily data dataset with the population dataset"""

        # Change the population age ranges to the COVID daily data ones
        age_range_translations = {'0-4': '0-9', '5-9': '0-9', '10-14': '10-19', '15-19': '10-19', '20-24': '20-29',
                                  '25-29': '20-29', '30-34': '30-39', '35-39': '30-39', '40-44': '40-49',
                                  '45-49': '40-49', '50-54': '50-59', '55-59': '50-59', '60-64': '60-69',
                                  '65-69': '60-69', '70-74': '70-79', '75-79': '70-79', '80-84': '80+', '85-89': '80+',
                                  '≥90': '80+', 'Total': 'total'}
        self.population_df['age_range'] = self.population_df['age_range'].replace(age_range_translations)
        self.population_df = self.population_df.groupby(['age_range', 'autonomous_region']).sum().reset_index()

        # Replace the M, F, total columns by a single 'gender' column
        self.population_df = self.population_df.melt(id_vars=['autonomous_region', 'age_range'],
                                                     value_vars=['M', 'F', 'total'],
                                                     var_name='gender')

        # Merge the COVID dataset with the population data
        covid_population_df = pd.merge(self.df, self.population_df, on=['autonomous_region', 'age_range', 'gender']) \
            .rename(columns={'value': 'population'})
        covid_population_df['date'] = pd.to_datetime(covid_population_df['date'])
        self.df = covid_population_df.set_index('date')

    def process_and_store_cases(self):
        """Create a DataFrame with all the data related to the cases"""
        # Get only the cases from the dataset
        cases_df = self.df.copy()[
            ['gender', 'age_range', 'autonomous_region', 'new_cases', 'total_cases', 'population']]

        # Calculate the cases per population
        cases_df['new_cases_per_population'] = 100000 * cases_df['new_cases'] / cases_df['population']
        cases_df['total_cases_per_population'] = 100000 * cases_df['total_cases'] / cases_df['population']

        # CI last 14 days
        cases_ci = cases_df.groupby(['autonomous_region', 'gender', 'age_range'])['new_cases_per_population'].rolling(
            '14D', min_periods=1).sum()
        cases_df = pd.merge(cases_df, cases_ci, on=['autonomous_region', 'date', 'gender', 'age_range']).rename(
            columns={'new_cases_per_population_x': 'new_cases_per_population',
                     'new_cases_per_population_y': 'ci_last_14_days'})
        cases_df['inverted_ci'] = cases_df['ci_last_14_days'].apply(lambda x: 100000 / x if x > 10 else 10000)

        # Daily, weekly and monthly increase
        increase_cases_df_1d = cases_df.groupby(['autonomous_region', 'gender', 'age_range'])['new_cases'].rolling(
            '7D').mean().rolling(2).apply(DailyCOVIDData.calculate_increase_percentage, raw=True)
        increase_cases_df_7d = cases_df.groupby(['autonomous_region', 'gender', 'age_range'])['new_cases'].rolling(
            '14D').mean().rolling(8).apply(DailyCOVIDData.calculate_increase_percentage, raw=True)
        increase_cases_df_30d = cases_df.groupby(['autonomous_region', 'gender', 'age_range'])['new_cases'].rolling(
            '60D').mean().rolling(31).apply(DailyCOVIDData.calculate_increase_percentage, raw=True)

        increase_cases_percentages = pd.DataFrame(
            {'daily_increase': increase_cases_df_1d, 'weekly_increase': increase_cases_df_7d,
             'monthly_increase': increase_cases_df_30d})
        cases_df = pd.merge(cases_df, increase_cases_percentages,
                            on=['autonomous_region', 'date', 'age_range', 'gender'])

        # New cases moving average
        new_cases_ma_1w = cases_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            'new_cases_per_population'].rolling('8D').mean()
        new_cases_ma_2w = cases_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            'new_cases_per_population'].rolling('15D').mean()
        new_cases_ma = pd.DataFrame({'new_cases_ma_1w': new_cases_ma_1w, 'new_cases_ma_2w': new_cases_ma_2w})
        cases_df = pd.merge(cases_df, new_cases_ma, on=['autonomous_region', 'date', 'age_range', 'gender'])

        cases_df = cases_df.drop(columns=['population'])

        # Store the data
        self.db_write.store_data('cases', cases_df.reset_index().to_dict('records'))

    def process_and_store_deaths(self):
        """Create a DataFrame with all the data related to the deaths"""
        # Get only the deaths from the dataset
        deaths_df = self.df.copy()[['gender', 'age_range', 'autonomous_region', 'new_deaths', 'total_deaths',
                                    'new_cases', 'total_cases', 'population']]

        # Calculate the deaths per population
        deaths_df['new_deaths_per_population'] = 100000 * deaths_df['new_deaths'] / deaths_df['population']
        deaths_df['total_deaths_per_population'] = 100000 * deaths_df['total_deaths'] / deaths_df['population']

        # Daily, weekly and monthly increase
        increase_deaths_df_1d = deaths_df.groupby(['autonomous_region', 'gender', 'age_range'])['new_deaths'].rolling(
            '2D').apply(DailyCOVIDData.calculate_increase_percentage, raw=True)
        increase_deaths_df_7d = deaths_df.groupby(['autonomous_region', 'gender', 'age_range'])['new_deaths'].rolling(
            '8D').apply(DailyCOVIDData.calculate_increase_percentage, raw=True)
        increase_deaths_df_14d = deaths_df.groupby(['autonomous_region', 'gender', 'age_range'])['new_deaths'].rolling(
            '15D').apply(DailyCOVIDData.calculate_increase_percentage, raw=True)
        increase_deaths_df_30d = deaths_df.groupby(['autonomous_region', 'gender', 'age_range'])['new_deaths'].rolling(
            '31D').apply(DailyCOVIDData.calculate_increase_percentage, raw=True)

        increase_deaths_percentages = pd.DataFrame(
            {'daily_increase': increase_deaths_df_1d, 'weekly_increase': increase_deaths_df_7d,
             'two_weeks_increase': increase_deaths_df_14d, 'monthly_increase': increase_deaths_df_30d})
        deaths_df = pd.merge(deaths_df, increase_deaths_percentages,
                             on=['autonomous_region', 'date', 'age_range', 'gender'])

        # New deaths moving average
        new_deaths_ma_1w = deaths_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            'new_deaths_per_population'].rolling('8D').mean()
        new_deaths_ma_2w = deaths_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            'new_deaths_per_population'].rolling('15D').mean()
        new_deaths_ma = pd.DataFrame({'new_deaths_ma_1w': new_deaths_ma_1w, 'new_deaths_ma_2w': new_deaths_ma_2w})
        deaths_df = pd.merge(deaths_df, new_deaths_ma, on=['autonomous_region', 'date', 'age_range', 'gender'])

        # Mortality percentage
        deaths_df['new_cases_per_population'] = 100000 * deaths_df['new_cases'] / deaths_df['population']
        new_cases_ma_2w = deaths_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            'new_cases_per_population'].rolling('15D').mean()
        new_cases_ma_2w_df = pd.DataFrame({'new_cases_ma_2w': new_cases_ma_2w})
        deaths_df = pd.merge(deaths_df, new_cases_ma_2w_df, on=['autonomous_region', 'date', 'age_range', 'gender'])
        deaths_df['mortality_2w'] = 100 * (deaths_df['new_deaths_ma_2w'] / deaths_df['new_cases_ma_2w']). \
            replace(np.nan, 0)
        deaths_df['mortality_total'] = 100 * (deaths_df['total_deaths'] / deaths_df['total_cases']).replace(np.nan, 0)

        deaths_df = deaths_df.drop(
            columns=['new_cases_ma_2w', 'new_cases_per_population', 'new_cases', 'total_cases', 'population'])

        # Store the data
        self.db_write.store_data('deaths', deaths_df.reset_index().to_dict('records'))

    def process_and_store_hospitalizations(self):
        """Create a DataFrame with all the data related to the hospitalizations"""
        # Get only the hospitalizations from the dataset
        hospitalizations_df = self.df.copy()[
            ['gender', 'age_range', 'autonomous_region', 'new_hospitalizations', 'total_hospitalizations',
             'new_ic_hospitalizations', 'total_ic_hospitalizations', 'new_cases', 'total_cases', 'population']]

        # Calculate the hospitalizations per population
        hospitalizations_df['new_hospitalizations_per_population'] = 100000 * hospitalizations_df[
            'new_hospitalizations'] / hospitalizations_df['population']
        hospitalizations_df['total_hospitalizations_per_population'] = 100000 * hospitalizations_df[
            'total_hospitalizations'] / hospitalizations_df['population']
        hospitalizations_df['new_ic_hospitalizations_per_population'] = 100000 * hospitalizations_df[
            'new_ic_hospitalizations'] / hospitalizations_df['population']
        hospitalizations_df['total_ic_hospitalizations_per_population'] = 100000 * hospitalizations_df[
            'total_ic_hospitalizations'] / hospitalizations_df['population']

        # Daily, weekly and monthly increase
        increase_hospitalizations_df_1d = hospitalizations_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            ['new_hospitalizations', 'new_ic_hospitalizations']].rolling('2D'). \
            apply(DailyCOVIDData.calculate_increase_percentage, raw=True)
        increase_hospitalizations_df_7d = hospitalizations_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            ['new_hospitalizations', 'new_ic_hospitalizations']].rolling('8D'). \
            apply(DailyCOVIDData.calculate_increase_percentage, raw=True)
        increase_hospitalizations_df_14d = hospitalizations_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            ['new_hospitalizations', 'new_ic_hospitalizations']].rolling('15D'). \
            apply(DailyCOVIDData.calculate_increase_percentage, raw=True)
        increase_hospitalizations_df_30d = hospitalizations_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            ['new_hospitalizations', 'new_ic_hospitalizations']].rolling('31D'). \
            apply(DailyCOVIDData.calculate_increase_percentage, raw=True)

        increase_hospitalizations_percentages = pd.DataFrame(
            {'hospitalizations_daily_increase': increase_hospitalizations_df_1d['new_hospitalizations'],
             'hospitalizations_weekly_increase': increase_hospitalizations_df_7d['new_hospitalizations'],
             'hospitalizations_two_weeks_increase': increase_hospitalizations_df_14d['new_hospitalizations'],
             'hospitalizations_monthly_increase': increase_hospitalizations_df_30d['new_hospitalizations'],
             'ic_daily_increase': increase_hospitalizations_df_1d['new_ic_hospitalizations'],
             'ic_weekly_increase': increase_hospitalizations_df_7d['new_ic_hospitalizations'],
             'ic_two_weeks_increase': increase_hospitalizations_df_14d['new_ic_hospitalizations'],
             'ic_monthly_increase': increase_hospitalizations_df_30d['new_ic_hospitalizations']})
        hospitalizations_df = pd.merge(hospitalizations_df, increase_hospitalizations_percentages,
                                       on=['autonomous_region', 'date', 'age_range', 'gender'])

        # New hospitalizations moving average
        new_hospitalizations_ma_1w = hospitalizations_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            ['new_hospitalizations_per_population', 'new_ic_hospitalizations_per_population']].rolling('8D').mean()
        new_hospitalizations_ma_2w = hospitalizations_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            ['new_hospitalizations_per_population', 'new_ic_hospitalizations_per_population']].rolling('15D').mean()
        new_hospitalizations_ma = pd.DataFrame(
            {'new_hospitalizations_ma_1w': new_hospitalizations_ma_1w['new_hospitalizations_per_population'],
             'new_hospitalizations_ma_2w': new_hospitalizations_ma_2w['new_hospitalizations_per_population'],
             'new_ic_ma_1w': new_hospitalizations_ma_1w['new_ic_hospitalizations_per_population'],
             'new_ic_ma_2w': new_hospitalizations_ma_2w['new_ic_hospitalizations_per_population']})
        hospitalizations_df = pd.merge(hospitalizations_df, new_hospitalizations_ma,
                                       on=['autonomous_region', 'date', 'age_range', 'gender'])

        # Hospitalization percentage
        hospitalizations_df['new_cases_per_population'] = \
            100000 * hospitalizations_df['new_cases'] / hospitalizations_df['population']
        new_cases_ma_2w = hospitalizations_df.groupby(['autonomous_region', 'gender', 'age_range'])[
            'new_cases_per_population'].rolling('15D').mean()
        new_cases_ma_2w_df = pd.DataFrame({'new_cases_ma_2w': new_cases_ma_2w})
        hospitalizations_df = pd.merge(hospitalizations_df, new_cases_ma_2w_df,
                                       on=['autonomous_region', 'date', 'age_range', 'gender'])
        hospitalizations_df['hospitalization_ratio_2w'] = 100 * (
                hospitalizations_df['new_hospitalizations_ma_2w'] / hospitalizations_df['new_cases_ma_2w']).replace(
            np.nan, 0)
        hospitalizations_df['hospitalization_ratio_total'] = 100 * (
                hospitalizations_df['total_hospitalizations'] / hospitalizations_df['total_cases']).replace(np.nan,
                                                                                                            0)
        hospitalizations_df['hospitalization_ic_ratio_2w'] = 100 * (
                hospitalizations_df['new_ic_ma_2w'] / hospitalizations_df['new_cases_ma_2w']).replace(np.nan, 0)
        hospitalizations_df['hospitalization_ic_ratio_total'] = 100 * (
                hospitalizations_df['total_ic_hospitalizations'] / hospitalizations_df['total_cases']).replace(
            np.nan, 0)

        hospitalizations_df = hospitalizations_df.drop(
            columns=['new_cases_ma_2w', 'new_cases_per_population', 'new_cases', 'total_cases', 'population'])

        # Store the data
        self.db_write.store_data('hospitalizations', hospitalizations_df.reset_index().to_dict('records'))


class VaccinationData:
    """Vaccination campaign progress in Spain"""

    def __init__(self):
        """Load the dataset"""
        # Connection to the extracted data database for reading, and to the analyzed data for writing
        self.db_read = MongoDatabase(MongoDatabase.extracted_db_name)
        self.db_write = MongoDatabase(MongoDatabase.analyzed_db_name)
        self.df_vaccination_general = self.db_read.read_data('vaccination_general')

    def __calculate_vaccinated_percentage__(self):
        """Calculate the percentage of vaccinated people"""
        population_df = self.db_read.read_data('population_ar', {'age_range': 'total'}, ['autonomous_region', 'total'])
        df_vaccination_join = pd.merge(self.df_vaccination_general, population_df, on='autonomous_region')
        df_vaccination_join['percentage_fully_vaccinated'] = \
            100 * df_vaccination_join['number_fully_vaccinated_people'] / df_vaccination_join['total']
        df_vaccination_join['percentage_at_least_single_dose'] = \
            100 * df_vaccination_join['number_at_least_single_dose_people'] / df_vaccination_join['total']
        df_vaccination_join = df_vaccination_join.drop(columns=['total'])
        self.df_vaccination_general = df_vaccination_join.replace({np.nan: None})

    def __calculate_vaccination_deltas__(self):
        """Calculate the number of new vaccinations each day, as well as the moving average"""
        df = self.df_vaccination_general.sort_values(['date', 'autonomous_region']).replace({None: np.nan})\
            .set_index('date')
        df['new_vaccinations'] = df.groupby(['autonomous_region'])['number_fully_vaccinated_people'].diff()
        new_vaccinations_ma = df.groupby('autonomous_region')['new_vaccinations'].rolling('7D').mean()
        self.df_vaccination_general = pd.merge(df, new_vaccinations_ma, on=['autonomous_region', 'date'])\
            .rename(columns={'new_vaccinations_x': 'new_vaccinations', 'new_vaccinations_y': 'new_vaccinations_ma_7d'})\
            .reset_index()\
            .replace({np.nan: None})

    def __move_ages_data__(self):
        """Just move the ages data from the extracted to the analyzed database"""
        vaccination_collections_names = ['vaccination_ages_single', 'vaccination_ages_complete']
        for collection_name in vaccination_collections_names:
            vaccination_collection = self.db_write.db.get_collection(collection_name)
            vaccination_collection.delete_many({})
            vaccination_collection.insert_many(self.db_read.db.get_collection(collection_name).find({}))

    def move_data(self):
        """Calculate the vaccination percentage and move the data"""
        self.__calculate_vaccinated_percentage__()
        self.__calculate_vaccination_deltas__()
        self.db_write.store_data('vaccination_general', self.df_vaccination_general.to_dict('records'))
        self.__move_ages_data__()


class SymptomsData:
    """Most common symptoms"""

    spanish_translation = {'aki': 'Infección aguda de riñón', 'dhiarrea': 'Diarrea',
                           'other_respiratory': 'Otras afecciones respiratorias', 'vomit': 'Vómitos',
                           'dyspnoea': 'Disnea', 'fever': 'Fiebre', 'ards': 'Síndrome de dificultad respiratoria aguda',
                           'cough': 'Tos', 'sore_throat': 'Dolor de garganta'}

    def __init__(self):
        """Load the dataset"""
        # Connection to the extracted data database for reading, and to the analyzed data for writing
        self.db_read = MongoDatabase(MongoDatabase.extracted_db_name)
        self.db_write = MongoDatabase(MongoDatabase.analyzed_db_name)

        self.symptoms_df = self.db_read.read_data('clinic_description', {'date': dt(2020, 5, 29)},
                                                  ['symptom', 'patients.total.percentage'])

    def move_data(self):
        """Get the total percentage and store the data in the analyzed database"""
        self.__transform_data__()
        self.__store_data__()

    def __transform_data__(self):
        """Get only the total percentage and translate the symptoms to Spanish"""
        self.symptoms_df['percentage'] = self.symptoms_df['patients'].apply(lambda x: x['total']['percentage'])
        self.symptoms_df = self.symptoms_df.drop(columns='patients')

        # Translate the symptoms to Spanish
        self.symptoms_df['symptom'] = self.symptoms_df['symptom'].replace(SymptomsData.spanish_translation)

    def __store_data__(self):
        """Store the processed data in the database"""
        self.db_write.store_data('symptoms', self.symptoms_df.to_dict('records'))


class DeathCauses:
    """Death causes in Spain"""

    age_range_translations = {'0-1': '0-9', '0-4': '0-9', '1-4': '0-9', '5-9': '0-9', '10-14': '10-19',
                              '15-19': '10-19', '20-24': '20-29',
                              '25-29': '20-29', '30-34': '30-39', '35-39': '30-39', '40-44': '40-49',
                              '45-49': '40-49', '50-54': '50-59', '55-59': '50-59', '60-64': '60-69',
                              '65-69': '60-69', '70-74': '70-79', '75-79': '70-79', '80-84': '80+', '85-89': '80+',
                              '90-94': '80+', '95+': '80+', '≥90': '80+', 'Total': 'total'}

    def __init__(self):
        """Load the datasets"""
        # Connection to the extracted data database for reading, and to the analyzed data for writing, as well as
        # for reading the aggregated deaths
        self.db_read = MongoDatabase(MongoDatabase.extracted_db_name)
        self.db_write = MongoDatabase(MongoDatabase.analyzed_db_name)

        # Load the death causes
        self.death_causes_df = self.db_read.read_data('death_causes')

        # Load the COVID deaths until now. If it hasn't been yet a year since 15th March 2020, get the deaths until
        # today and calculate the proportional number to 365 days. If it's been more than one year,
        # get the number of deaths of the last 365 days.
        today = dt.today() - td(
            days=7)  # the today deaths data might not be available yet, so we'll use the data from one week ago
        today = dt(today.year, today.month, today.day)  # remove the time from the today datetime object

        if today - dt(2020, 3, 15) >= td(days=365):
            # Get the number of deaths of the last 365 days
            deaths_one_year_ago_df = self.db_write.read_data('deaths', {'autonomous_region': 'España', 'date':
                today - td(days=365)}, ['age_range', 'total_deaths', 'gender'])
            deaths_today_df = self.db_write.read_data('deaths', {'autonomous_region': 'España', 'date': today},
                                                      ['age_range', 'total_deaths', 'gender'])
            self.covid_deaths_df = deaths_today_df
            self.covid_deaths_df['total_deaths'] = \
                self.covid_deaths_df['total_deaths'] - deaths_one_year_ago_df['total_deaths']
        else:
            # Get the number of deaths until today, and calculate the remaining proportion to complete a year
            deaths_today_df = self.db_write.read_data('deaths', {'autonomous_region': 'España', 'date': today},
                                                      ['age_range', 'total_deaths', 'gender'])
            proportion = 365 / (today - dt(2020, 3, 15)).days
            self.covid_deaths_df = deaths_today_df
            self.covid_deaths_df['total_deaths'] = self.covid_deaths_df['total_deaths'] * proportion

    def process_and_store_data(self):
        """Analyze the data, calculate some new variables, and store the results to the database"""
        self.__calculate_top_10_death_causes__()
        self.__store_data__()

    def __calculate_top_10_death_causes__(self):
        """Calculate the top 10 death causes in Spain and the percentage of total deaths whose cause was COVID"""
        # Use the same age ranges in the three dataframes
        self.death_causes_df['age_range'] = self.death_causes_df['age_range']. \
            replace(DeathCauses.age_range_translations)
        self.death_causes_df = self.death_causes_df.groupby(['age_range', 'death_cause', 'gender']).sum().reset_index()

        # Get "all causes" death cause and then remove it
        all_causes_sum_df = self.death_causes_df[self.death_causes_df['death_cause'] == 'Todas las causas'].copy()
        death_causes_df = self.death_causes_df[self.death_causes_df['death_cause'] != 'Todas las causas']

        # Group the 2018 death causes with the COVID-19 deaths
        self.covid_deaths_df['death_cause'] = 'COVID-19'
        death_causes_total = pd.concat([self.covid_deaths_df, death_causes_df])

        # Get the top 10 death causes for each age range and gender
        death_causes_top_10 = death_causes_total.sort_values(['age_range', 'total_deaths', 'gender'],
                                                             ascending=False).groupby(['age_range', 'gender']).head(10)
        death_causes_top_10['total_deaths'] = death_causes_top_10['total_deaths'].round().astype("int")
        self.death_causes_top_10 = death_causes_top_10

        # Calculate the percentage of deaths produced by COVID
        covid_vs_all_deaths = pd.merge(self.covid_deaths_df, all_causes_sum_df, on=['age_range', 'gender']).rename(
            columns={'total_deaths_x': 'covid_deaths', 'total_deaths_y': 'other_deaths'}).drop(
            columns=['death_cause_y', 'death_cause_x'])
        covid_vs_all_deaths['covid_percentage'] = 100 * covid_vs_all_deaths['covid_deaths'] / (
                covid_vs_all_deaths['covid_deaths'] + covid_vs_all_deaths['other_deaths'])
        self.covid_vs_all_deaths = covid_vs_all_deaths

    def __store_data__(self):
        """Store the top death causes and the COVID deaths percentage in the database"""
        mongo_data_top_death_causes = self.death_causes_top_10.to_dict('records')
        collection = 'top_death_causes'
        self.db_write.store_data(collection, mongo_data_top_death_causes)

        mongo_data_covid_vs_all_deaths = self.covid_vs_all_deaths.to_dict('records')
        collection = 'covid_vs_all_deaths'
        self.db_write.store_data(collection, mongo_data_covid_vs_all_deaths)


class PopulationPyramidVariation:
    """Create a table with the population pyramid variation suffered due to COVID"""
    age_range_translations = {'0-1': '0-9', '0-4': '0-9', '1-4': '0-9', '5-9': '0-9', '10-14': '10-19',
                              '15-19': '10-19', '20-24': '20-29',
                              '25-29': '20-29', '30-34': '30-39', '35-39': '30-39', '40-44': '40-49',
                              '45-49': '40-49', '50-54': '50-59', '55-59': '50-59', '60-64': '60-69',
                              '65-69': '60-69', '70-74': '70-79', '75-79': '70-79', '80-84': '80+', '85-89': '80+',
                              '90-94': '80+', '95+': '80+', '≥90': '80+', 'Total': 'total'}

    def __init__(self):
        """Load the datasets"""
        # Connection to the extracted data database for reading the population data, and to the analyzed data for
        # writing, as well as for reading the aggregated deaths
        self.db_read = MongoDatabase(MongoDatabase.extracted_db_name)
        self.db_write = MongoDatabase(MongoDatabase.analyzed_db_name)

        # Load the data
        self.covid_deaths_df = self.db_write.read_data('covid_vs_all_deaths', None,
                                                       ['gender', 'age_range', 'covid_deaths'])
        self.population_df = self.db_read.read_data('population_ar', {'autonomous_region': 'España'},
                                                    ['age_range', 'M', 'F', 'total'])

    def process_and_store_data(self):
        self.__transform_data__()
        self.__store_data__()

    def __transform_data__(self):
        """Create the table with the joined data from both DataFrames"""
        # Replace the age range in the population DataFrame
        self.population_df['age_range'] = self.population_df['age_range']. \
            replace(PopulationPyramidVariation.age_range_translations)
        self.population_df = self.population_df.groupby('age_range').sum().reset_index()

        # Melt the gender columns in the population DataFrame
        self.population_df = self.population_df.melt(id_vars='age_range', var_name='gender')

        # Group horizontally the two DataFrames together
        self.population_pyramid_covid_df = \
            pd.merge(self.population_df, self.covid_deaths_df,
                     on=['age_range', 'gender']).rename(columns={'value': 'alive_population'})
        self.population_pyramid_covid_df['alive_population'] = \
            self.population_pyramid_covid_df['alive_population'] - self.population_pyramid_covid_df['covid_deaths']

    def __store_data__(self):
        """Store the data in the database"""
        mongo_data = self.population_pyramid_covid_df.to_dict('records')
        collection = 'population_pyramid_variation'
        self.db_write.store_data(collection, mongo_data)


class DiagnosticTests:
    """Dataset with the number of diagnostic tests made each day on each Autonomous Region"""

    def __init__(self):
        """Load the datasets"""
        # Connection to the extracted data database for reading, and to the analyzed data for writing
        self.db_read = MongoDatabase(MongoDatabase.extracted_db_name)
        self.db_write = MongoDatabase(MongoDatabase.analyzed_db_name)

        # Load the diagnostic tests, Spanish population and COVID cases datasets
        self.diagnostic_tests_df = self.db_read.read_data('diagnostic_tests')
        self.population_df = self.db_read.read_data('population_ar', {'age_range': 'total'},
                                                    ['autonomous_region', 'total'])

    def __process_dataset__(self):
        """
            Get the data for the whole country, the total number of tests, the average positivity, and the number of
            total tests per 100k inhabitants.
        """
        # Number of tests and average positivity in the whole country
        diagnostics_grouped = self.diagnostic_tests_df.groupby('date')
        diagnostics_total_tests = diagnostics_grouped['total_diagnostic_tests'].sum()
        diagnostics_avg_positivity = diagnostics_grouped['positivity'].mean()
        diagnostics_df_total = pd.merge(diagnostics_total_tests, diagnostics_avg_positivity,
                                        on='date').reset_index()

        diagnostics_df_total['autonomous_region'] = 'España'
        self.diagnostic_tests_df = pd.concat([self.diagnostic_tests_df, diagnostics_df_total])
        self.diagnostic_tests_df = self.diagnostic_tests_df.sort_values(by=['date', 'autonomous_region'])

        # Moving average for positivity (the positivity line is very sharp)
        diagnostics_df = self.diagnostic_tests_df.set_index('date')
        positivity_ma = diagnostics_df.groupby('autonomous_region')['positivity'].rolling('14D').mean()
        self.diagnostic_tests_df = pd.merge(diagnostics_df, positivity_ma, on=['autonomous_region', 'date'])\
            .rename(columns={'positivity_x': 'positivity', 'positivity_y': 'positivity_ma_14d'}) \
            .reset_index() \
            .replace({np.nan: None})

        # Number of total tests
        diagnostic_tests_df_total = self.diagnostic_tests_df[['date', 'autonomous_region', 'total_diagnostic_tests']] \
            .groupby(['date', 'autonomous_region']).sum().groupby('autonomous_region').cumsum().reset_index()
        self.diagnostic_tests_df = pd.merge(self.diagnostic_tests_df, diagnostic_tests_df_total,
                                            on=['date', 'autonomous_region']).rename(
            columns={'total_diagnostic_tests_x': 'new_diagnostic_tests',
                     'total_diagnostic_tests_y': 'total_diagnostic_tests'})

        # Moving average for number of total tests
        diagnostics_df = self.diagnostic_tests_df.set_index('date')
        diagnostics_ma = diagnostics_df.groupby('autonomous_region')['new_diagnostic_tests'].rolling('14D').mean()
        self.diagnostic_tests_df = pd.merge(diagnostics_df, diagnostics_ma, on=['autonomous_region', 'date'])\
            .rename(columns={'new_diagnostic_tests_x': 'new_diagnostic_tests',
                             'new_diagnostic_tests_y': 'new_diagnostic_tests_ma_14d'}) \
            .reset_index() \
            .replace({np.nan: None})

        # Average positivity for each Autonomous Region
        diagnostic_tests_df_avg_positivity = self.diagnostic_tests_df[
            ['date', 'autonomous_region', 'positivity']].groupby(['date', 'autonomous_region']).sum().groupby(
            'autonomous_region')
        avg_positivity_df = diagnostic_tests_df_avg_positivity.cumsum().rename(columns={'positivity': 'sum'})
        avg_positivity_df['count'] = diagnostic_tests_df_avg_positivity.cumcount()
        avg_positivity_df['average_positivity'] = avg_positivity_df['sum'] / avg_positivity_df['count']
        avg_positivity_df = avg_positivity_df.drop(columns=['sum', 'count'])
        self.diagnostic_tests_df = pd.merge(self.diagnostic_tests_df, avg_positivity_df,
                                            on=['date', 'autonomous_region'])

        # Total tests / 100 000 inhabitants
        diagnostics_population_df = pd.merge(self.diagnostic_tests_df, self.population_df, on='autonomous_region') \
            .rename(columns={'total': 'population'})
        diagnostics_population_df['total_tests_per_population'] = 100000 * diagnostics_population_df[
            'total_diagnostic_tests'] / diagnostics_population_df['population']
        self.diagnostic_tests_df = diagnostics_population_df.drop(columns='population')

    def __store_data__(self):
        """Store the processed dataset in the database"""
        mongo_data = self.diagnostic_tests_df.replace({np.nan: None}).to_dict('records')
        collection = 'diagnostic_tests'
        self.db_write.store_data(collection, mongo_data)

    def process_and_store(self):
        """Analyze the data, calculate some new variables, and store the results to the database"""
        self.__process_dataset__()
        self.__store_data__()


class OutbreaksDescription:
    """Outbreaks description in Spain"""

    def __init__(self):
        """Load the dataset"""
        # Connection to the extracted data database for reading, and to the analyzed data for writing
        self.db_read = MongoDatabase(MongoDatabase.extracted_db_name)
        self.db_write = MongoDatabase(MongoDatabase.analyzed_db_name)

        # Load the outbreaks description
        self.outbreaks_description_df = self.db_read.read_data('outbreaks_description')

    def move_data(self):
        """Just move the data from the extracted to the analyzed database"""
        self.__store_data__()

    def __store_data__(self):
        """Store the outbreaks description in the database"""
        mongo_data = self.outbreaks_description_df.to_dict('records')
        collection = 'outbreaks_description'
        self.db_write.store_data(collection, mongo_data)


class HospitalsPressure:
    """Hospitals pressure in Spain"""

    def __init__(self):
        """Load the dataset"""
        # Connection to the extracted data database for reading, and to the analyzed data for writing
        self.db_read = MongoDatabase(MongoDatabase.extracted_db_name)
        self.db_write = MongoDatabase(MongoDatabase.analyzed_db_name)

        # Load the hospitals pressure data
        self.hospitals_pressure = self.db_read.read_data('hospitals_pressure',
                                                         projection=['autonomous_region', 'date',
                                                                     'hospitalized_patients', 'beds_percentage',
                                                                     'ic_patients', 'ic_beds_percentage'])

    def __aggregate_data__(self):
        """Calculate the data for the whole country"""
        pressure_grouped = self.hospitals_pressure.groupby('date')
        pressure_patients = pressure_grouped[['hospitalized_patients', 'ic_patients']].sum()
        pressure_beds_percentage = pressure_grouped[['beds_percentage', 'ic_beds_percentage']].mean()
        hospitals_pressure_total = pd.merge(pressure_patients, pressure_beds_percentage, on='date').reset_index()
        hospitals_pressure_total['autonomous_region'] = 'España'
        self.hospitals_pressure = pd.concat([self.hospitals_pressure, hospitals_pressure_total])
        self.hospitals_pressure = self.hospitals_pressure.sort_values(by=['date', 'autonomous_region'])

    def __calculate_ma__(self):
        """Calculate the moving average for the beds percentages, since the data can be very sharp"""
        hospitals_pressure_df = self.hospitals_pressure.set_index('date')
        hospitals_ma = hospitals_pressure_df.groupby('autonomous_region')[['beds_percentage', 'ic_beds_percentage']]\
            .rolling('14D').mean()
        self.hospitals_pressure = pd.merge(hospitals_pressure_df, hospitals_ma, on=['autonomous_region', 'date'])\
            .rename(columns={'beds_percentage_x': 'beds_percentage', 'beds_percentage_y': 'beds_percentage_ma_14d',
                             'ic_beds_percentage_x': 'ic_beds_percentage',
                             'ic_beds_percentage_y': 'ic_beds_percentage_ma_14d'}) \
            .reset_index() \
            .replace({np.nan: None})

    def transform_and_store(self):
        """Analyze the data, calculate some new variables, and store the results to the database"""
        self.__aggregate_data__()
        self.__calculate_ma__()
        self.__store_data__()

    def __store_data__(self):
        """Store the outbreaks description in the database"""
        mongo_data = self.hospitals_pressure.to_dict('records')
        collection = 'hospitals_pressure'
        self.db_write.store_data(collection, mongo_data)


class TransmissionIndicators:
    """
        Transmission indicators in Spain: cases with unknown contact,
        identified contacts per case and asymptomatic cases percentage.
    """

    def __init__(self):
        """Load the dataset"""
        # Connection to the extracted data database for reading, and to the analyzed data for writing
        self.db_read = MongoDatabase(MongoDatabase.extracted_db_name)
        self.db_write = MongoDatabase(MongoDatabase.analyzed_db_name)

        # Load the transmission indicators data
        self.transmission_indicators = self.db_read.read_data('transmission_indicators')

    def __transform_data__(self):
        """Get only the desired data and transform it to a single-level hierarchy"""
        ti_df = self.transmission_indicators
        ti_df['cases_unknown_contact'] = ti_df['transmission_indicators'].apply(
            lambda x: x['cases_unknown_contact']['percentage'])
        ti_df['identified_contacts_per_case'] = ti_df['transmission_indicators'].apply(
            lambda x: x['identified_contacts_per_case']['median'])
        ti_df['asymptomatic_percentage'] = ti_df['transmission_indicators'].apply(
            lambda x: x['asymptomatic_percentage'])
        self.transmission_indicators = ti_df.drop(columns='transmission_indicators')

    def __aggregate_data__(self):
        """Calculate the data for the whole country"""
        grouped_data = self.transmission_indicators.groupby('date')
        grouped_df = grouped_data.mean().reset_index()
        grouped_df['autonomous_region'] = 'España'
        self.transmission_indicators = pd.concat([self.transmission_indicators, grouped_df])
        self.transmission_indicators = self.transmission_indicators.sort_values(by=['date', 'autonomous_region'])

    def transform_and_store(self):
        """Transform, aggregate, and store the data"""
        self.__transform_data__()
        self.__aggregate_data__()
        self.__store_data__()

    def __store_data__(self):
        """Store the outbreaks description in the database"""
        mongo_data = self.transmission_indicators.to_dict('records')
        collection = 'transmission_indicators'
        self.db_write.store_data(collection, mongo_data)


class DataAnalysisTaskGroup(TaskGroup):
    """TaskGroup that analyzes all the downloaded and extracted data."""

    def __init__(self, dag):
        # Instantiate the TaskGroup
        super(DataAnalysisTaskGroup, self) \
            .__init__("data_analysis",
                      tooltip="Analyze all the downloaded and extracted data",
                      dag=dag)

        # Instantiate the operators
        PythonOperator(task_id='analyze_cases_data',
                       python_callable=DataAnalysisTaskGroup.analyze_daily_cases,
                       task_group=self,
                       dag=dag)

        analyze_deaths_data_op = PythonOperator(task_id='analyze_deaths_data',
                                                python_callable=DataAnalysisTaskGroup.analyze_daily_deaths,
                                                task_group=self,
                                                dag=dag)

        PythonOperator(task_id='analyze_hospitalizations_data',
                       python_callable=DataAnalysisTaskGroup.analyze_daily_hospitalizations,
                       task_group=self,
                       dag=dag)

        analyze_death_causes_op = PythonOperator(task_id='analyze_death_causes',
                                                 python_callable=DataAnalysisTaskGroup.analyze_death_causes,
                                                 task_group=self,
                                                 dag=dag)

        analyze_pyramid_variation_op = PythonOperator(task_id='analyze_population_pyramid_variation',
                                                      python_callable=DataAnalysisTaskGroup.
                                                      analyze_population_pyramid_variation,
                                                      task_group=self,
                                                      dag=dag)

        analyze_deaths_data_op >> analyze_death_causes_op >> analyze_pyramid_variation_op

        PythonOperator(task_id='move_outbreaks_description',
                       python_callable=DataAnalysisTaskGroup.move_outbreaks_description,
                       task_group=self,
                       dag=dag)

        PythonOperator(task_id='analyze_hospitals_pressure',
                       python_callable=DataAnalysisTaskGroup.analyze_hospitals_pressure,
                       task_group=self,
                       dag=dag)

        PythonOperator(task_id='analyze_diagnostic_tests_data',
                       python_callable=DataAnalysisTaskGroup.analyze_diagnostic_tests,
                       task_group=self,
                       dag=dag)

        PythonOperator(task_id='move_transmission_indicators',
                       python_callable=DataAnalysisTaskGroup.move_transmission_indicators,
                       task_group=self,
                       dag=dag)

        PythonOperator(task_id='move_symptoms',
                       python_callable=DataAnalysisTaskGroup.move_symptoms_data,
                       task_group=self,
                       dag=dag)

        PythonOperator(task_id='analyze_vaccination',
                       python_callable=DataAnalysisTaskGroup.analyze_vaccination_data,
                       task_group=self,
                       dag=dag)

    @staticmethod
    def analyze_daily_cases():
        """Analyze the cases data in the daily COVID dataset"""
        data = DailyCOVIDData()
        data.process_and_store_cases()

    @staticmethod
    def analyze_daily_deaths():
        """Analyze the deaths data in the daily COVID dataset"""
        data = DailyCOVIDData()
        data.process_and_store_deaths()

    @staticmethod
    def analyze_daily_hospitalizations():
        """Analyze the hospitalizations data in the daily COVID dataset"""
        data = DailyCOVIDData()
        data.process_and_store_hospitalizations()

    @staticmethod
    def analyze_death_causes():
        """Extract the top 10 death causes and compare them with COVID-19"""
        data = DeathCauses()
        data.process_and_store_data()

    @staticmethod
    def analyze_population_pyramid_variation():
        """Analyze the variation of the population pyramid due to COVID"""
        data = PopulationPyramidVariation()
        data.process_and_store_data()

    @staticmethod
    def move_outbreaks_description():
        """Move the outbreaks description from the extracted to the analyzed database"""
        data = OutbreaksDescription()
        data.move_data()

    @staticmethod
    def analyze_hospitals_pressure():
        """Analyze the hospitals pressure data"""
        data = HospitalsPressure()
        data.transform_and_store()

    @staticmethod
    def analyze_diagnostic_tests():
        """Analyze the diagnostic tests data"""
        data = DiagnosticTests()
        data.process_and_store()

    @staticmethod
    def move_transmission_indicators():
        """Move the transmission indicators data from the extracted to the analyzed database"""
        data = TransmissionIndicators()
        data.transform_and_store()

    @staticmethod
    def move_symptoms_data():
        """Move the symptoms data from the extracted to the analyzed database"""
        data = SymptomsData()
        data.move_data()

    @staticmethod
    def analyze_vaccination_data():
        """Analyze the vaccination data"""
        data = VaccinationData()
        data.move_data()
