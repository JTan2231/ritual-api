create database if not exists ritual;
use ritual;

CREATE TABLE if not exists user (
    user_id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    password VARCHAR(255) NOT NULL,
    UNIQUE KEY unique_username (username)
);

create table if not exists ethos (
    ethos_id int auto_increment primary key,
    user_id int not null,
    core varchar(4096) not null,
    summary varchar(4096),
    feedback varchar(4096),

    constraint fk_user_ethos foreign key (user_id) references user(user_id)
);

CREATE TABLE if not exists activity (
    activity_id INT AUTO_INCREMENT PRIMARY KEY,
    user_id int not null,
    name varchar(256) NOT NULL,
    activity_begin datetime NOT NULL,
    activity_end datetime not null,
    memo VARCHAR(512) NOT NULL,

    constraint fk_user foreign key (user_id) references user(user_id)
);

create table if not exists goal (
    goal_id int auto_increment primary key,
    user_id int not null,
    name varchar(256) not null,
    description varchar(4096) not null,

    constraint fk_user_goal foreign key (user_id) references user(user_id),
    constraint uc_user_id_name unique (user_id, name)
);

create table if not exists subgoal (
    subgoal_id int auto_increment primary key,
    goal_id int not null,
    user_id int not null,
    name varchar(256) not null,
    description varchar(4096) not null,

    constraint fk_user_subgoal foreign key (user_id) references user(user_id),
    constraint subgoal_user_id_name unique (user_id, name)
);
